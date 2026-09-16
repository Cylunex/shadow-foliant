"""Refresh and persist an actionable market gate at each scheduled report node."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
REPORT_SLOTS = frozenset({"10:15", "11:25", "14:35"})


def refresh_market_add_signal(
    report_slot: str,
    *,
    snapshot_saver: Callable[[str, dict[str, Any]], Any],
    builder: Optional[Callable[..., dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Force fresh core-index and A500 inputs and save a fail-closed snapshot."""
    slot = str(report_slot or "").strip()
    if slot not in REPORT_SLOTS:
        raise ValueError("market_add_signal_report_slot_invalid")
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    if builder is None:
        from analysis.market_add_signal import build

        builder = build
    try:
        signal = dict(builder(force=True) or {})
    except Exception as exc:
        signal = {
            "action": "unknown", "level": "unknown",
            "reason": f"{type(exc).__name__}；市场总闸刷新失败，默认保持仓位。",
            "source_status": "failed",
            "source_failure_code": "market_add_signal_refresh_failed",
        }

    breadth = signal.get("breadth") if isinstance(signal.get("breadth"), dict) else {}
    failure_code = str(signal.get("source_failure_code") or "").strip()
    if not failure_code and int(signal.get("available") or 0) < 3:
        failure_code = "core_indices_incomplete"
    if not failure_code and not breadth.get("available"):
        failure_code = str(
            breadth.get("failure_code") or "a500_breadth_unavailable"
        )
    if failure_code:
        from analysis.market_add_signal import fail_closed

        signal = fail_closed(signal, failure_code)
    else:
        signal["source_status"] = "success"
        signal["source_failure_code"] = None

    signal.update({
        "date": current.date().isoformat(),
        "updated_at": current.isoformat(timespec="seconds"),
        "report_slot": slot,
    })
    snapshot_saver("_market_add_signal", signal)
    return signal
