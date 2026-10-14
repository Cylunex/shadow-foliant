"""Shared daily quote validity and reference-price display semantics."""
from datetime import datetime
from decimal import Decimal
import math
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def current_day_price(quote, now):
    if isinstance(quote.get("price"), bool):
        return None
    try:
        price = float(quote.get("price"))
        instant = datetime.fromisoformat(str(quote.get("as_of") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if (not math.isfinite(price) or price <= 0 or instant.tzinfo is None
            or quote.get("freshness") not in {"actionable", "closing_current"}):
        return None
    local = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    return price if instant.astimezone(SHANGHAI).date() == local.date() and instant <= local else None


def format_reference_price(price):
    """Keep sub-cent fund quotes visible instead of rounding them to stock ticks."""
    value = Decimal(str(price))
    places = max(2, min(4, -value.normalize().as_tuple().exponent))
    return f"¥{value:.{places}f}"
