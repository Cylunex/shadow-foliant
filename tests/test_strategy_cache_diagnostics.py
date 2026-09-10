from __future__ import annotations

from pathlib import Path

from selection import strategy_cache


def test_failure_codes_are_stable_and_cache_only_replays_persisted_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(strategy_cache, "_cache_dir", lambda: str(tmp_path))
    assert strategy_cache.classify_failure("HTTP 403 Forbidden") == "http_403"
    assert strategy_cache.classify_failure(TimeoutError("timeout")) == "timeout"
    strategy_cache.record_failure("低估值", "HTTP 429 rate limit")

    ok, frame, message = strategy_cache.cached(
        "低估值", lambda: (_ for _ in ()).throw(AssertionError("must not fetch")),
        cache_only=True,
    )

    assert ok is False and frame is None
    assert "last_failure=http_429" in message
    assert list(Path(tmp_path).glob("*.failure.json"))

