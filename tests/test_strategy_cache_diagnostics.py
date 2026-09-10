from __future__ import annotations

from pathlib import Path

from selection import strategy_cache


def test_failure_codes_are_stable_and_cache_only_replays_persisted_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(strategy_cache, "_cache_dir", lambda: str(tmp_path))
    assert strategy_cache.classify_failure("HTTP 403 Forbidden") == "http_403"
    assert strategy_cache.classify_failure(TimeoutError("timeout")) == "timeout"
    assert strategy_cache.classify_failure(
        "缓存缺失;last_failure=circuit_open:pywencai circuit is open"
    ) == "circuit_open"
    assert strategy_cache.classify_failure(
        "RemoteDisconnected: Remote end closed connection without response"
    ) == "source_unavailable"
    assert strategy_cache.classify_failure(
        "主力资金源不可用（通用候选缺少主力资金字段，已拒绝冒充）"
    ) == "source_unavailable"
    strategy_cache.record_failure("低估值", "HTTP 429 rate limit")

    ok, frame, message = strategy_cache.cached(
        "低估值", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")),
        cache_only=True,
    )

    assert ok is False and frame is None
    assert "last_failure=http_429" in message
    assert list(Path(tmp_path).glob("*.failure.json"))


def test_append_only_wencai_repair_wins_without_overwriting_original():
    class Store:
        def __init__(self):
            self.saved = []

        def formal_selection(self, run_id):
            artifacts = {
                "wencai_strategy_runs": {"payload": {
                    "strategies": {"低估值": {"failure_code": "cache_missing"}},
                }},
            }
            for artifact_type, payload in self.saved:
                artifacts[artifact_type] = {"payload": payload, "artifact_id": "repair-id"}
            return {"run_id": run_id, "artifacts": artifacts}

        def save_selection_artifact(self, _run_id, artifact_type, payload):
            self.saved.append((artifact_type, payload))
            return "repair-id"

    store = Store()
    repair = {"strategies": {"低估值": {"failure_code": "circuit_open"}}}
    assert strategy_cache.save_artifact(store, "run-1", repair) == "repair-id"
    formal = store.formal_selection("run-1")
    assert strategy_cache.artifact_payload(formal["artifacts"]) == repair
    assert store.saved == [("wencai_strategy_runs_repair", repair)]

    recovered = {"strategies": {"低估值": {"status": "ready", "picks": []}}}
    assert strategy_cache.save_artifact(store, "run-1", recovered) == "repair-id"
    formal = store.formal_selection("run-1")
    assert strategy_cache.artifact_payload(formal["artifacts"]) == recovered
    assert store.saved[1][0].startswith("wencai_strategy_runs_repair_")
