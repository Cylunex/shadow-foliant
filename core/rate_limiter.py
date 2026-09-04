"""Compatibility import: all entry points share one provider governor."""
from data.rate_limiter import (  # noqa: F401
    throttle, throttled, throttled_session, host_key, _gap_for, _fold_host,
)
