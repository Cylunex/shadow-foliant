"""Cross-layer checks for recurring operations failures, without live notifications."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from application.scheduled_notification import ScheduledNotificationService
from application.scheduled_report_contract import REPORT_MAX_LINES
from data.research_store import ResearchStore
from jobs.automation_config import REGISTRY
from jobs.task_health import effective_task_run, summarize_task_health
from scripts.foliant_scheduled_snapshot import qq_payload
from scripts.verify_release_readiness import verify


def _report():
    return {
        "trading_day": {"date": "2026-10-13"},
        "formal_selection": {"status": "complete", "formal_top5": [
            {"symbol": f"600{i:03d}", "name": f"正式{i}"} for i in range(5)]},
        "independent_selection": {"status": "complete", "top5": [
            {"symbol": f"000{i:03d}", "name": f"独立{i}"} for i in range(5)]},
        "trade_plans": {"holding_actions_authority": {"status": "current"},
                        "holding_actions": [
                            {"symbol": f"601{i:03d}", "name": f"观察{i}",
                             "action": "sell", "reason": "规则观察"} for i in range(20)]},
    }


@pytest.fixture(params=("sqlite", "postgres"))
def report_store(request, tmp_path):
    if request.param == "sqlite":
        yield ResearchStore(str(tmp_path / "research.db"), connect_fn=sqlite3.connect,
                            is_postgres=False, ensure_schema=True)
        return
    if os.getenv("RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("PostgreSQL integration is opt-in")
    from core.db_compat import _PGConnection
    from psycopg2 import sql
    schema = "test_report_closure_" + uuid.uuid4().hex
    admin = _PGConnection()
    admin._conn.cursor().execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.commit()

    def connect(_name=None):
        conn = _PGConnection()
        conn._conn.cursor().execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        yield ResearchStore(connect_fn=connect, is_postgres=True, ensure_schema=True)
    finally:
        admin._conn.cursor().execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_rendered_full_report_claims_through_real_http_route_and_deduplicates(report_store, monkeypatch):
    from application import scheduled_notification as module
    from webui.scheduled_snapshot_routes import register_scheduled_snapshot_routes
    service = ScheduledNotificationService(
        store=report_store, clock=lambda: datetime.fromisoformat("2026-10-13T11:25:00+08:00"))
    monkeypatch.setattr(module, "ScheduledNotificationService", lambda: service)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.agent_identity = SimpleNamespace(agent_id="fixture")
        return await call_next(request)

    register_scheduled_snapshot_routes(app, agent_result=lambda value: value,
                                       agent_error=lambda exc: {"error": str(exc)})
    client = TestClient(app)
    payload = qq_payload(_report())
    assert payload["delivered_lines"] == REPORT_MAX_LINES
    claim = {key: payload[key] for key in (
        "payload_hash", "original_lines", "delivered_lines", "category", "version")}
    claim["notification_slot"] = "2026-10-13T11:25+08:00"
    base = "/api/machine/v1/agent/scheduled-snapshot/notification-"
    response = client.post(base + "claim", json=claim)
    assert response.status_code == 200
    assert response.json()["data"]["should_send"] is True
    start = {key: claim[key] for key in ("payload_hash", "notification_slot")}
    assert client.post(base + "start", json=start).json()["data"]["started"] is True
    assert client.post(base + "start", json=start).json()["data"]["started"] is False
    assert client.post(base + "finish", json=start | {
        "status": "delivered", "http_status": 200}).json()["data"]["recorded"] is True
    replay = client.post(base + "claim", json=claim).json()["data"]
    assert replay["prior_sent"] is True and replay["should_send"] is False
    claim["delivered_lines"] = REPORT_MAX_LINES + 1
    assert client.post(base + "claim", json=claim).status_code == 422
    with pytest.raises(ValueError, match="line_count_invalid"):
        service.claim(slot=start["notification_slot"], actor_id="fixture",
                      **{key: claim[key] for key in payload if key in claim})


def test_paused_optional_jobs_are_visible_without_false_failure():
    tasks = [{"name": name, "enabled": False, "core": REGISTRY[name]["core"],
              "last_run": {"status": "error", "finished_at": "2026-09-20T13:00:00+08:00"}}
             for name in ("weekend_portfolio", "weekly_analysis")]
    result = summarize_task_health(tasks)
    assert result["failed_recent"] == [] and result["disabled_core"] == []
    assert result["paused"] == ["weekend_portfolio", "weekly_analysis"]
    tasks.append({"name": "unified_selection", "enabled": False, "core": True})
    assert summarize_task_health(tasks)["disabled_core"] == ["unified_selection"]


def test_latest_run_compares_real_instants_including_legacy_shanghai_times():
    scheduled = {"status": "error", "finished_at": "2026-10-13T11:00:00+08:00"}
    manual = {"status": "success", "finished_at": "2026-10-13T04:00:00Z"}
    assert effective_task_run({"last_run": scheduled, "manual_run": manual}) is manual
    scheduled["finished_at"] = "2026-10-13 13:00:00"
    assert effective_task_run({"last_run": scheduled, "manual_run": manual}) is scheduled
    manual["status"] = "running"
    assert effective_task_run({"last_run": scheduled, "manual_run": manual}) is manual


def test_recent_failure_limit_prioritizes_latest_actual_failure():
    tasks = [{"name": name, "enabled": True, "last_run": {
        "status": "error", "finished_at": stamp}} for name, stamp in (
            ("older", "2026-10-13T11:00:00+08:00"),
            ("newer", "2026-10-13T04:00:00Z"))]
    result = summarize_task_health(tasks, recent_limit=1)
    assert result["failed_count"] == 2
    assert result["failed_recent"][0]["name"] == "newer"


def test_release_gate_requires_exact_revision_and_real_health():
    revision = "a" * 40
    payload = {"ready": True, "revision": revision, "checks": {
        name: {"ok": True} for name in ("database", "manual_queue", "revision")}}
    assert verify(payload, expected_revision=revision)
    for change in ({"ready": False}, {"ready": "true"}, {"revision": "b" * 40},
                   {"checks": {}}, {"revision": "b" * 40, "note": revision}):
        assert not verify(payload | change, expected_revision=revision)
    broken = deepcopy(payload)
    broken["checks"]["manual_queue"]["ok"] = False
    assert not verify(broken, expected_revision=revision)
    assert verify({"ready": False, "checks": {"warehouse": False}})
    assert not verify({"ready": "false", "checks": {"warehouse": False}})
    script = Path(__file__).resolve().parents[1] / "scripts/verify_release_readiness.py"
    result = subprocess.run([sys.executable, str(script), "--expected-revision", revision],
                            input='{"ready": "private-invalid-response"}', text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 1
    assert "private-invalid-response" not in result.stderr
    assert "private-invalid-response" not in result.stdout
