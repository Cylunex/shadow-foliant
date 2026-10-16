"""Keep high-frequency monitor silence separate from ordinary reports."""

def is_monitor_source(source: str) -> bool:
    return any("monitor" in part.lower() for part in source.split("."))


def routine_delivery_allowed(category: str, source: str,
                             idempotency_key: str | None = None) -> bool:
    if category not in {"report", "archive", "daily_summary", "system_error"}:
        return False
    # The protected CLI still needs its own slot certification. A malformed
    # scheduled call must not fall through to the ordinary report path.
    if (source == "scripts.foliant_scheduled_snapshot"
            or str(idempotency_key or "").startswith("scheduled-qq:")):
        return False
    # Cover both package imports and the legacy sys.path module aliases.
    return not is_monitor_source(source)
