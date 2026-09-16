from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from application.market_signal_refresh import refresh_market_add_signal
from portfolio_policy import is_usable_market_add_signal


SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize("slot", ["10:15", "11:25", "14:35"])
def test_each_report_slot_forces_and_persists_independent_freshness(slot):
    calls = []
    saved = []
    hour, minute = map(int, slot.split(":"))
    now = datetime(2026, 9, 16, hour, minute, tzinfo=SHANGHAI)

    def builder(*, force=False):
        calls.append(force)
        return {
            "action": "hold", "level": "hold", "available": 5,
            "breadth": {"available": True, "covered": 480},
        }

    signal = refresh_market_add_signal(
        slot, builder=builder, now=now,
        snapshot_saver=lambda symbol, payload: saved.append((symbol, payload.copy())),
    )

    assert calls == [True]
    assert saved[0][0] == "_market_add_signal"
    assert signal["report_slot"] == slot
    assert signal["updated_at"] == now.isoformat(timespec="seconds")
    assert signal["source_status"] == "success"
    assert signal["source_failure_code"] is None
    assert is_usable_market_add_signal(signal, now=now)


def test_breadth_failure_is_saved_with_source_code_and_fails_closed():
    now = datetime(2026, 9, 16, 14, 35, tzinfo=SHANGHAI)
    saved = []
    signal = refresh_market_add_signal(
        "14:35", now=now,
        builder=lambda **_kwargs: {
            "action": "buy", "level": "buy", "available": 5,
            "breadth": {
                "available": False, "failure_code": "a500_quotes_failed",
                "reason": "quote batch unavailable",
            },
        },
        snapshot_saver=lambda symbol, payload: saved.append(payload.copy()),
    )

    assert saved
    assert signal["action"] == "unknown"
    assert signal["source_status"] == "failed"
    assert signal["source_failure_code"] == "a500_quotes_failed"
    assert not is_usable_market_add_signal(signal, now=now)


def test_all_fixed_jobs_refresh_their_matching_report_slot():
    source = (Path(__file__).resolve().parents[1] / "jobs" / "jobs_hub.py").read_text()
    for slot in ("10:15", "11:25", "14:35"):
        assert f"_refresh_market_gate('{slot}')" in source
