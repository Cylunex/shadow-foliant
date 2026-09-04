"""Host-wide HTTP admission: stable priority, bounded wait, quota and circuit.

Lock/state files contain only provider IDs and counters, never URLs or credentials.
Native Baostock and paid-provider quota guards remain authoritative as well.
All processes must share FOLIANT_RUNTIME_DATA_DIR on the same host.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

# Operational safety ceilings, not claims about vendors' purchased quotas.
PROVIDERS = {
    "zzshare": (0, 1., 30000), "baostock": (0, .2, 45000),
    "tushare": (0, 1., 10000), "tencent": (1, .5, 10000),
    "sina": (1, 1., 5000), "cninfo": (1, 1., 3000),
    "eltdx": (1, .1, 10000), "tdx_python": (1, .1, 10000),
    "tdx": (1, .1, 10000),  # additional shared upstream-family ceiling
    "mairui": (2, .5, 500), "moma": (2, .5, 500),
    "eastmoney": (2, 3., 3000), "ths": (2, 2., 2000),
    "pywencai": (2, 2., 500), "eastmoney_saas": (2, 3., 500),
    "akshare": (3, 3., 2000), "baidu": (3, 2., 1000),
    "cls": (2, 2., 2000), "tickflow": (2, 6., 500),
    "jsl": (3, 3., 500), "easy_tdx": (3, .2, 5000), "mootdx": (3, .2, 5000),
    "default": (3, 2., 1000),
}
_HOSTS = (("ai-saas", "eastmoney_saas"), ("iwencai", "pywencai"),
          ("eastmoney", "eastmoney"), ("10jqka", "ths"), ("hexin", "ths"),
          ("sinajs", "sina"), ("sina.com", "sina"), ("gtimg", "tencent"),
          ("qq.com", "tencent"), ("cninfo", "cninfo"), ("tushare", "tushare"),
          ("mairui", "mairui"), ("moma", "moma"), ("zzshare", "zzshare"),
          ("baidu", "baidu"), ("cls.cn", "cls"), ("tickflow", "tickflow"), ("jisilu", "jsl"))
_local = threading.local()


class SourceBudgetUnavailable(RuntimeError):
    """Safe routing signal. No request payload is included."""


def host_provider(url: str) -> str:
    host = (urlparse(str(url)).hostname or "").lower()
    return next((provider for fragment, provider in _HOSTS if fragment in host), "default")


def operational_interval(provider: str) -> float:
    floor = PROVIDERS.get(provider, PROVIDERS["default"])[1]
    try:
        value = float(os.getenv("RATE_LIMIT_" + provider.upper(), floor))
        return max(floor, value) if math.isfinite(value) else floor
    except (ValueError, TypeError):
        return floor


def _directory() -> Path:
    from _bootstrap import DB_DIR
    root = os.getenv("FOLIANT_RUNTIME_DATA_DIR") or DB_DIR
    directory = Path(root).expanduser().resolve() / "provider_governor"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


@contextmanager
def provider_slot(provider: str, *, wait_seconds: float = 10., interval: float = 0., charge: bool = True):
    """Serialize actual calls across threads/processes, including timed-out workers.

    Nested adapters share the outer reservation. A timeout at the route caller
    cannot release this slot until the underlying network request really exits.
    File corruption fails closed instead of resetting the daily budget.
    """
    provider = provider if provider in PROVIDERS else "default"
    active = getattr(_local, "active", set())
    if provider in active:
        yield
        return
    timeout = max(0., min(float(wait_seconds), 30.))
    deadline = time.monotonic() + timeout
    path = _directory() / (hashlib.sha256(provider.encode()).hexdigest()[:16] + ".json")
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as handle:
        locked = False
        try:
            while not locked:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SourceBudgetUnavailable(f"{provider}: concurrency_wait_exceeded")
                    time.sleep(.025)
            raw = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                if path.exists() and not raw.strip():
                    raise ValueError("empty persisted quota state")
                state = json.loads(raw) if raw else {}
                if not isinstance(state, dict):
                    raise ValueError("invalid state")
            except (ValueError, TypeError):
                raise SourceBudgetUnavailable(f"{provider}: quota_state_invalid") from None
            now = time.time()
            day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            if state.get("day") != day:
                state.update(day=day, count=0)
            if float(state.get("cooldown_until", 0)) > now:
                raise SourceBudgetUnavailable(f"{provider}: circuit_open")
            ceiling = PROVIDERS[provider][2]
            try:
                limit = max(0, min(ceiling, int(os.getenv("SOURCE_DAILY_LIMIT_" + provider.upper(), ceiling))))
            except ValueError:
                limit = ceiling
            if int(state.get("count", 0)) >= limit:
                raise SourceBudgetUnavailable(f"{provider}: daily_budget_exhausted")
            gap = max(operational_interval(provider), interval)
            wait = max(0., float(state.get("last_started", 0)) + gap - now)
            if wait > deadline - time.monotonic():
                raise SourceBudgetUnavailable(f"{provider}: rate_wait_exceeded")
            if wait:
                time.sleep(wait)
            state.update(last_started=time.time(), count=int(state.get("count", 0)) + int(charge))

            def persist():
                import tempfile
                fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as output:
                        json.dump(state, output, sort_keys=True)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)

            persist()  # Charge before I/O; crashes must not refund requests.
            _local.active = active | {provider}
            try:
                yield
            except Exception:
                if not charge:
                    raise
                failures = int(state.get("failures", 0)) + 1
                state["failures"] = failures
                if failures >= 3:
                    state["cooldown_until"] = time.time() + min(1800, 60 * 2 ** min(failures - 3, 5))
                persist()
                raise
            else:
                if charge:
                    state.update(failures=0, cooldown_until=0)
                    persist()
            finally:
                _local.active = active
        finally:
            if locked:
                fcntl.flock(handle, fcntl.LOCK_UN)


def guarded_session(session, key=None):
    """Guard Session.send for all HTTP methods; nested same-provider calls share a reservation."""
    original = session.send
    if getattr(original, "_provider_guarded", False):
        return session

    def send(request, **kwargs):
        provider = key or host_provider(request.url)
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (5, 15)
        with provider_slot(provider):
            response = original(request, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                raise SourceBudgetUnavailable(f"{provider}: http_{response.status_code}")
            return response
    send._provider_guarded = True
    session.send = send
    return session


def budget_snapshot():
    """Bounded public counters; never return file paths, URLs or configuration values."""
    out = {}
    for provider, (tier, _, ceiling) in PROVIDERS.items():
        try:
            limit = max(0, min(ceiling, int(os.getenv("SOURCE_DAILY_LIMIT_" + provider.upper(), ceiling))))
        except ValueError:
            limit = ceiling
        value = {"priority_tier": tier, "operational_daily_ceiling": ceiling,
                 "configured_daily_limit": limit,
                 "minimum_interval_seconds": operational_interval(provider),
                 "max_concurrency": 1, "count": None, "status": "not_observed"}
        path = _directory() / (hashlib.sha256(provider.encode()).hexdigest()[:16] + ".json")
        if path.exists():
            try:
                with path.with_suffix(".lock").open("a+") as handle:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    state = json.loads(path.read_text())
                    value.update(count=int(state.get("count", 0)), day=state.get("day"),
                                 status="cooling" if state.get("cooldown_until", 0) > time.time() else "ready",
                                 cooldown_until=state.get("cooldown_until", 0))
                    if value["day"] != datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
                        value["count"] = 0
                    if value["status"] == "ready" and value["count"] >= limit:
                        value["status"] = "budget_exhausted"
            except BlockingIOError:
                value["status"] = "in_flight"
            except (ValueError, OSError, TypeError):
                value["status"] = "state_unreadable"
        if limit == 0:
            value["status"] = "budget_exhausted"
        out[provider] = value
    return out
