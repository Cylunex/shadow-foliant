from __future__ import annotations

from pathlib import Path
import time
import sqlite3

import pytest

from core import cache
from application.research_artifacts import append_ai_annotation, build_research_artifact
from data import runtime_capabilities
from data.research_store import ResearchStore
from data.source_contracts import SourceCooldownActive, source_call
from jobs.isolated_runtime import run_isolated_task


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _quick_task():
    return None


def _slow_task():
    time.sleep(5)


def test_research_artifact_has_evidence_freshness_and_invalidation() -> None:
    artifact = build_research_artifact(
        subject="600519", run_id="run-example",
        facts={"market": {"dataset_id": "dataset-example", "as_of": "2026-08-21",
                           "quality_status": "ok"}},
        provenance={"decision_at": "2026-08-22T09:00:00+08:00",
                    "market_as_of": "2026-08-21", "run_id": "run-example"},
        data_quality={"level": "high"}, formal=True,
    )
    assert artifact["schema_version"] == "research-artifact-v1"
    assert artifact["formal"] is True
    assert artifact["evidence"][0]["dataset_id"] == "dataset-example"
    assert artifact["evidence"][0]["freshness"] == "current"
    assert artifact["invalidation_conditions"]
    assert artifact["payload_hash"]


def test_formal_artifact_identity_is_stable_for_same_decision() -> None:
    values = dict(
        subject="600519", run_id="run-example", facts={},
        provenance={"decision_at": "2026-08-22T16:00:00+08:00"},
        data_quality={"level": "formal"}, formal=True,
        created_at="2026-08-22T16:00:00+08:00",
    )
    assert build_research_artifact(**values)["artifact_id"] == build_research_artifact(
        **values
    )["artifact_id"]


def test_ai_annotation_is_separate_and_non_authoritative(tmp_path) -> None:
    database = tmp_path / "artifacts.db"

    def connect(_name=""):
        return sqlite3.connect(database)

    conn = connect()
    conn.execute(
        """CREATE TABLE research_artifacts (
             artifact_id TEXT PRIMARY KEY,formal INTEGER,payload_hash TEXT,payload TEXT)"""
    )
    conn.execute(
        """CREATE TABLE research_artifact_annotations (
             annotation_id TEXT PRIMARY KEY,artifact_id TEXT,annotation_kind TEXT,
             payload_hash TEXT,payload TEXT,created_at TEXT)"""
    )
    conn.execute(
        "INSERT INTO research_artifacts VALUES (?,?,?,?)",
        ("ra-example", 1, "formal-hash", '{"formal":true}'),
    )
    conn.commit()
    conn.close()
    annotation = append_ai_annotation(
        artifact_id="ra-example", annotation_kind="llm-review",
        payload={"recommended_order": ["000001"]}, connect_fn=connect,
    )
    assert annotation["authoritative"] is False
    conn = connect()
    assert conn.execute(
        "SELECT payload_hash FROM research_artifacts WHERE artifact_id='ra-example'"
    ).fetchone()[0] == "formal-hash"
    assert conn.execute("SELECT COUNT(*) FROM research_artifact_annotations").fetchone()[0] == 1
    conn.close()


def test_runtime_capability_snapshot_never_exposes_secret_values(monkeypatch) -> None:
    monkeypatch.setenv("ZZSHARE_TOKEN", "secret-example-value")
    monkeypatch.setattr(runtime_capabilities, "_stored_states", lambda: {
        ("zzshare", "daily_market"): {
            "last_success_at": "2026-08-22T16:00:00+08:00",
            "updated_at": "2026-08-22T16:00:00+08:00",
        }
    })
    snapshot = runtime_capabilities.capability_snapshot()
    text = str(snapshot)
    assert "secret-example-value" not in text
    assert snapshot["providers"]["zzshare"]["daily_market"]["configured"] is True
    assert snapshot["dataset_routes"]["external_reference"]["policy"] == "reference_only"


def test_source_call_rejects_active_cooldown_without_provider_work(monkeypatch) -> None:
    class RateLimitError(RuntimeError):
        pass

    runtime_capabilities._reset_for_tests()
    monkeypatch.setattr(runtime_capabilities, "_schedule_persist", lambda *_args: None)
    runtime_capabilities.record_failure("zzshare", "daily_market", RateLimitError())
    with pytest.raises(SourceCooldownActive):
        with source_call("zzshare", "daily_market"):
            raise AssertionError("provider work must not start")


def test_dataset_generation_does_not_advance_for_identical_publication() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE research_dataset_publications (
             capability TEXT PRIMARY KEY,generation INTEGER,dataset_id TEXT,
             effective_as_of TEXT,published_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE research_dataset_publication_history (
             capability TEXT,generation INTEGER,dataset_id TEXT,
             effective_as_of TEXT,published_at TEXT,
             PRIMARY KEY(capability,generation))"""
    )
    cur = conn.cursor()
    assert ResearchStore._advance_generation(cur, "daily_market", "dataset-1", "2026-08-21") == 1
    assert ResearchStore._advance_generation(cur, "daily_market", "dataset-1", "2026-08-21") == 1
    assert ResearchStore._advance_generation(cur, "daily_market", "dataset-2", "2026-08-22") == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM research_dataset_publication_history"
    ).fetchone()[0] == 2
    conn.close()


def test_redis_is_not_probed_without_explicit_configuration(monkeypatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    cache._client = None
    cache._tried = False
    cache._down_until = 0.0
    assert cache._redis() is None
    assert cache._tried is True


def test_rate_slot_preserves_caller_exception(monkeypatch) -> None:
    class Redis:
        @staticmethod
        def set(*_args, **_kwargs):
            return True

    class CallerFailure(RuntimeError):
        pass

    monkeypatch.setattr(cache, "_redis", lambda: Redis())
    with pytest.raises(CallerFailure):
        with cache.rate_slot("provider.example", 0.01):
            raise CallerFailure("must pass through unchanged")


def test_isolated_runtime_completes_and_terminates_timeout() -> None:
    complete = run_isolated_task(
        "quick-example", _quick_task, (), {}, timeout_seconds=5, cancel_grace_seconds=1
    )
    assert complete["status"] == "complete"
    timed_out = run_isolated_task(
        "slow-example", _slow_task, (), {}, timeout_seconds=1, cancel_grace_seconds=1
    )
    assert timed_out["status"] == "timeout"
    assert timed_out["terminated"] is True


def test_migration_script_uses_portable_bounded_version_query() -> None:
    script = (REPOSITORY_ROOT / "scripts" / "migrate.sh").read_text(encoding="utf-8")
    assert "version=:'version'" not in script
    assert "^[0-9A-Za-z][0-9A-Za-z._-]*$" in script
    assert "WHERE version='$version'" in script
