from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from application.scheduled_snapshot import ScheduledSnapshotService


NOW = datetime(2026, 9, 10, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
ROOT = Path(__file__).resolve().parents[1]


class CalendarStore:
    def __init__(self, *, ready=True, coverage="2026-09-10", latest="2026-09-10"):
        self.value = {
            "ready": ready,
            "covered_provider_count": 2 if ready else 1,
            "coverage_through_date": coverage,
            "latest_confirmed_open_date": latest,
        }

    def calendar_consensus(self, _day, *, inclusive=False):
        assert inclusive is True
        return dict(self.value)


def selection(*, day="2026-09-10", with_wencai=True):
    top15 = [{
        "symbol": f"600{i:03d}", "name": f"候选{i}", "rank": i,
        "trade_plan": {"available": True, "action": "hold", "reason": "规则计划"},
    } for i in range(1, 16)]
    strategies = {
        name: {"strategy_id": f"wencai-{index}", "strategy_version": "v1",
               "status": "ready", "picks": [{"symbol": f"00000{index}", "name": name}]}
        for index, name in enumerate(("主力资金", "低价擒牛", "小市值", "净利增长", "低估值"), 1)
    } if with_wencai else {}
    return {
        "status": "complete",
        "warnings": [],
        "provenance": {"run_id": "formal-run", "market_as_of": day,
                       "input_manifest_id": "manifest", "policy_hash": "policy",
                       "code_revision": "revision"},
        "data": {
            "selection_date": day,
            "formal_top15": top15,
            "formal_top5": top15[:5],
            "references": {
                "wencai": {"executed_at": NOW.isoformat(), "strategies": strategies},
                "independent": {
                    "status": "ready", "strategy_id": "codex-independent",
                    "strategy_version": "codex-independent-v1", "strategy_hash": "fixed",
                    "manifest_id": "manifest", "input_snapshot_id": "independent-snapshot",
                    "market_as_of": "2026-09-09",
                    "weights": {"fundamental_quality": 30, "medium_trend": 25,
                                "valuation": 20, "flow_liquidity": 15,
                                "risk_discount": 10},
                    "top15": top15, "top5": top15[:5],
                    "independence_boundary": "immutable_manifest_inputs_only",
                },
            },
            "selection_comparison": {
                "availability": {"formal": True, "independent": True,
                                 "wencai": bool(with_wencai)},
                "pairwise": {}, "triple": None,
            },
        },
    }


def capsule():
    rows = [{"symbol": f"600{i:03d}", "industry": "示例", "themes": ["测试"]}
            for i in range(1, 16)]
    return {
        "capsule_id": "dc_example", "run_id": "formal-run",
        "opportunity_set": {"top15": rows, "top5": rows[:5]},
    }


def cockpit(**_kwargs):
    return {"status": "success", "meta": {"as_of": NOW.isoformat()}, "data": {
        "tasks": {"total": 10, "failed_recent": [], "disabled_core": [],
                  "running_manual": []},
        "holding_count": 2, "active_recommendation_count": 0,
        "active_signal_count": 0, "datahub": {}, "portfolio_policy": {},
        "strategy_deployment": {},
    }}


def context():
    return {"watermark": "same-watermark", "holdings": [
        {"code": "600001", "name": "持仓甲", "cost_price": 9, "quantity": 100},
        {"code": "000001", "name": "持仓乙", "cost_price": 10, "quantity": 200,
         "note": "must-not-be-projected"},
    ]}


def build_service(*, store=None, selection_value=None, quote_spy=None, context_reader=context):
    def quotes(symbols):
        if quote_spy is not None:
            quote_spy.append(list(symbols))
        return {symbol: {"name": symbol, "price": 10, "change_pct": 1,
                         "quote_time": NOW.isoformat(), "volume": 1000,
                         "amount_wan": 100, "limit_up": 11, "limit_down": 9}
                for symbol in symbols}

    return ScheduledSnapshotService(
        store=store or CalendarStore(),
        selection_reader=lambda: selection_value or selection(),
        cockpit_reader=cockpit,
        context_reader=context_reader,
        capsule_reader=capsule,
        quote_loader=quotes,
        clock=lambda: NOW,
    )


def test_snapshot_batches_top15_and_holdings_once_and_keeps_as_of():
    calls = []
    result = build_service(quote_spy=calls).read(owner_id="scheduled-agent")
    snapshot = result["data"]
    assert len(calls) == 1
    assert calls[0] == sorted({*(f"600{i:03d}" for i in range(1, 16)), "000001"})
    assert snapshot["quotes"]["batch_count"] == 1
    assert snapshot["quotes"]["requested_count"] == 16
    assert all(row["as_of"] == NOW.isoformat() for row in snapshot["quotes"]["rows"])
    assert snapshot["trade_plans"]["auto_execution"] is False
    assert len(snapshot["trade_plans"]["formal"]) == 15
    assert "note" not in snapshot["holdings"]["rows"][1]
    assert snapshot["independent_selection"]["status"] == "complete"
    assert len(snapshot["independent_selection"]["top5"]) == 5
    assert snapshot["quality"]["sections"]["independent_selection"] == "complete"


def test_unknown_calendar_never_uses_weekday_fallback():
    result = build_service(store=CalendarStore(ready=False, coverage="2026-09-09")).read(
        owner_id="scheduled-agent"
    )["data"]
    assert NOW.weekday() < 5
    assert result["trading_day"]["confirmed"] is False
    assert result["trading_day"]["is_trading_day"] is None
    assert result["trading_day"]["basis"] == "two_source_consensus_required"


def test_confirmed_closed_day_and_stale_formal_are_explicit():
    store = CalendarStore(latest="2026-09-09")
    result = build_service(store=store, selection_value=selection(day="2026-09-08")).read(
        owner_id="scheduled-agent"
    )["data"]
    assert result["trading_day"]["confirmed"] is True
    assert result["trading_day"]["is_trading_day"] is False
    assert result["formal_selection"]["status"] == "stale"
    assert result["status"] == "degraded"


def test_missing_wencai_does_not_change_formal_candidates():
    value = selection(with_wencai=False)
    expected = [row["symbol"] for row in value["data"]["formal_top15"]]
    result = build_service(selection_value=value).read(owner_id="scheduled-agent")["data"]
    assert [row["symbol"] for row in result["formal_selection"]["formal_top15"]] == expected
    assert result["wencai_reference"]["status"] == "missing"
    assert result["wencai_reference"]["reference_affects_membership"] is False


def test_holdings_watermark_change_fails_risk_plan_closed():
    reads = [context(), {**context(), "watermark": "changed"}]
    result = build_service(context_reader=lambda: reads.pop(0)).read(
        owner_id="scheduled-agent"
    )["data"]
    assert result["holdings"]["status"] == "complete"
    assert result["trade_plans"]["status"] == "stale"
    assert result["trade_plans"]["portfolio_risk"]["error_code"] == "holdings_changed_during_preview"


def test_route_requires_exact_scheduled_capability():
    from webui import access_control
    from webui.api_server import app

    token = "Bearer " + "x" * 40
    capabilities = set()

    class Authenticator:
        def authenticate(self, authorization):
            if authorization != token:
                raise ValueError("invalid")
            return SimpleNamespace(
                agent_id="scheduled-agent", owner_app="shadow-platform", audience="foliant",
                scopes=frozenset({"stock.portfolio.read"}), capabilities=frozenset(capabilities),
            )

    access_control._agent_authenticator = Authenticator()
    fake_result = {"summary": "ok", "resource_uri": "shadow://foliant/reports/scheduled-test",
                   "status": "complete", "provenance": {}, "warnings": [], "data": {}}
    with TestClient(app, base_url="https://stock.example.com") as client:
        denied = client.get("/api/machine/v1/agent/scheduled-snapshot",
                            headers={"Authorization": token})
        assert denied.status_code == 403
        capabilities.add("foliant.scheduled-report.read")
        with patch("application.scheduled_snapshot.ScheduledSnapshotService.read",
                   return_value=fake_result) as read:
            allowed = client.get("/api/machine/v1/agent/scheduled-snapshot",
                                 headers={"Authorization": token})
        assert allowed.status_code == 200
        read.assert_called_once_with(owner_id="scheduled-agent")


def test_cli_missing_config_is_structured_and_does_not_call_http(monkeypatch):
    from scripts import foliant_scheduled_snapshot as cli

    for name in ("FOLIANT_AGENT_BASE_URL", "FOLIANT_AGENT_TOKEN_FILE", "FOLIANT_AGENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with patch.object(cli.requests, "get") as get:
        result = cli.fetch_snapshot()
    assert result["status"] == "missing"
    assert result["error"]["code"] == "agent_base_url_missing"
    get.assert_not_called()


def test_cli_auth_failure_and_notification_never_leak_secrets(monkeypatch):
    from notify import notification_router
    from scripts import foliant_scheduled_snapshot as cli

    monkeypatch.setenv("FOLIANT_AGENT_BASE_URL", "https://private.example.invalid")
    monkeypatch.setenv("FOLIANT_AGENT_TOKEN", "super-secret-bearer")
    response = SimpleNamespace(status_code=403)
    with patch.object(cli.requests, "get", return_value=response):
        failure = cli.fetch_snapshot()
    rendered = str(failure)
    assert "private.example.invalid" not in rendered
    assert "super-secret-bearer" not in rendered

    monkeypatch.setenv("QQ_WEBHOOK_URL", "https://private.example.invalid/secret-hook")
    snapshot = selection()
    snapshot.update({
        "status": "degraded", "trading_day": {"date": "2026-09-10", "confirmed": True},
        "formal_selection": selection()["data"],
        "wencai_reference": {"ready_groups": 5},
        "holdings": {"count": 2, "status": "complete",
                     "error": "Bearer should-not-appear https://secret.invalid"},
        "trade_plans": {"status": "complete", "portfolio_risk": {"summary": "先观察"}},
    })
    captured = {}

    def send(_category, title, body, **_kwargs):
        captured.update(title=title, body=body)
        return {"qq": (False, "https://private.example.invalid/secret-hook failed")}

    with patch.object(notification_router, "send", side_effect=send):
        result = cli.send_qq(snapshot)
    assert result == {"requested": True, "sent": False, "channel": "qq",
                      "error_code": "qq_delivery_failed"}
    body = captured["body"]
    assert "private.example.invalid" not in body
    assert "should-not-appear" not in body
    assert "super-secret-bearer" not in body


def test_cli_absolute_path_from_external_cwd_sends_qq(tmp_path):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    marker = tmp_path / "qq-post.json"
    (hook_dir / "sitecustomize.py").write_text(
        """
import json
import os
import requests

class Response:
    status_code = 200
    def __init__(self, payload):
        self._payload = payload
    def json(self):
        return self._payload

def fake_get(*_args, **_kwargs):
    return Response({"data": {
        "schema_version": "scheduled-agent-snapshot-v1",
        "status": "degraded",
        "trading_day": {"date": "2026-09-10", "confirmed": False},
        "formal_selection": {"status": "complete", "formal_top15": [], "formal_top5": []},
        "wencai_reference": {"ready_groups": 0},
        "holdings": {"status": "complete", "count": 2},
        "trade_plans": {"status": "degraded", "portfolio_risk": {}},
    }})

def fake_post(_url, *, json=None, **_kwargs):
    with open(os.environ["FOLIANT_TEST_POST_MARKER"], "w", encoding="utf-8") as handle:
        handle.write(__import__("json").dumps(json, ensure_ascii=False))
    return Response({"ok": True})

requests.get = fake_get
requests.post = fake_post
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(hook_dir),
        "FOLIANT_TEST_POST_MARKER": str(marker),
        "FOLIANT_AGENT_BASE_URL": "https://agent.example.invalid",
        "FOLIANT_AGENT_TOKEN": "external-cwd-test-token-that-is-long-enough",
        "QQ_WEBHOOK_URL": "https://qq.example.invalid/private-hook",
        "SHADOW_LOG_TIMESTAMPS": "false",
    })
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "foliant_scheduled_snapshot.py"),
         "--send-qq"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["notification"] == {"requested": True, "sent": True, "channel": "qq"}
    assert json.loads(marker.read_text("utf-8"))["msgtype"] == "markdown"
    assert "external-cwd-test-token" not in completed.stdout
