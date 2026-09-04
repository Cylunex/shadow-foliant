"""Executable capability and safety contracts for external market-data sources.

The registry deliberately distinguishes provider-published hard limits from
Foliant's more conservative operational limits.  Callers must never infer that
an undocumented upstream quota is guaranteed merely because Foliant applies a
local delay.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
import math
import threading
import time
from typing import Dict, Iterator, Optional, Tuple


@dataclass(frozen=True)
class EndpointContract:
    provider: str
    endpoint: str
    capability: str
    auth: str
    hard_max_rows: Optional[int]
    page_size: Optional[int]
    min_interval_seconds: float
    max_concurrency: int
    timeout_seconds: float
    retries: int
    supports_pit: bool = False
    adjustment: str = "not_applicable"
    volume_unit: str = "not_applicable"
    amount_unit: str = "not_applicable"
    quota_basis: str = "operational"
    notes: str = ""
    daily_request_limit: Optional[int] = None

    def public_dict(self) -> dict:
        """Return configuration metadata that is always safe to log or expose."""
        return asdict(self)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return max(value, minimum) if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(int(os.getenv(name, str(default))), minimum)
    except (TypeError, ValueError):
        return default


def get_contract(provider: str, endpoint: str) -> EndpointContract:
    """Resolve one endpoint contract, applying only repository-safe env knobs."""
    key = (str(provider).lower(), str(endpoint).lower())
    base = _BASE_CONTRACTS.get(key)
    if base is None:
        raise KeyError(f"unknown source endpoint: {key[0]}.{key[1]}")
    prefix = f"SOURCE_{key[0]}_{key[1]}".upper().replace("-", "_")
    configured_interval = _env_float(
        f"{prefix}_MIN_INTERVAL_SECONDS", base.min_interval_seconds
    )
    configured_concurrency = _env_int(
        f"{prefix}_MAX_CONCURRENCY", base.max_concurrency
    )
    configured_retries = _env_int(f"{prefix}_RETRIES", base.retries + 1) - 1
    return EndpointContract(
        **{
            **asdict(base),
            "min_interval_seconds": max(base.min_interval_seconds, configured_interval),
            "max_concurrency": min(base.max_concurrency, configured_concurrency),
            "timeout_seconds": _env_float(
                f"{prefix}_TIMEOUT_SECONDS", base.timeout_seconds, minimum=0.1
            ),
            "retries": min(base.retries, configured_retries),
        }
    )


def contracts() -> Dict[str, Dict[str, dict]]:
    """Return the complete public capability matrix grouped by provider."""
    out: Dict[str, Dict[str, dict]] = {}
    for provider, endpoint in sorted(_BASE_CONTRACTS):
        out.setdefault(provider, {})[endpoint] = get_contract(provider, endpoint).public_dict()
    return out


_BASE_CONTRACTS: Dict[Tuple[str, str], EndpointContract] = {
    ("tushare", "dividend"): EndpointContract(
        "tushare", "dividend", "corporate_actions", "token_and_permission", None, None,
        1.0, 1, 15.0, 0, notes="Dividends only; no all-action completeness or holder-specific net tax guarantee"),
    ("tushare", "valuation"): EndpointContract(
        "tushare", "valuation", "valuation_snapshot", "token", 6000, 6000,
        1.0, 1, 15.0, 0, supports_pit=True, quota_basis="published",
        notes="daily_basic requires eligible points; total_mv/circ_mv are CNY 10000.",
    ),
    ("tencent", "valuation"): EndpointContract(
        "tencent", "valuation", "closing_valuation_snapshot", "none", None, 80,
        0.5, 1, 6.0, 0, notes="Require same-day quote timestamp after 15:00; no historical backfill.",
    ),
    ("eastmoney", "valuation"): EndpointContract(
        "eastmoney", "valuation", "closing_valuation_snapshot", "none", None, 80,
        0.5, 1, 6.0, 0, notes="Require f124 timestamp; f9 is not PE-TTM.",
    ),
    ("zzshare", "daily_symbol"): EndpointContract(
        "zzshare", "daily_symbol", "daily_ohlcv", "token", 1000, 1000,
        1.0, 1, 15.0, 2, adjustment="raw/qfq/hfq", volume_unit="runtime_validated",
        amount_unit="provider_native", quota_basis="published",
        notes="Use offset pagination beyond 1000 rows.",
    ),
    ("zzshare", "daily_market"): EndpointContract(
        "zzshare", "daily_market", "daily_market_snapshot", "token", 10000, 6000,
        1.0, 1, 20.0, 2, adjustment="raw/qfq/hfq", volume_unit="runtime_validated",
        amount_unit="runtime_validated_turnover", quota_basis="published",
        notes="Provider documents 10000 maximum and recommends 6000 for one market day.",
    ),
    ("zzshare", "minute"): EndpointContract(
        "zzshare", "minute", "minute_ohlcv", "token", 1000, 1000,
        1.0, 1, 15.0, 2, adjustment="raw", volume_unit="runtime_validated",
        amount_unit="provider_native", quota_basis="operational",
        notes="Foliant caps one request at 1000; upstream does not publish a larger guarantee.",
    ),
    ("zzshare", "realtime"): EndpointContract(
        "zzshare", "realtime", "realtime_quote", "token", None, None,
        3.1, 1, 15.0, 1, adjustment="raw", quota_basis="published",
        notes="Provider documents 20 calls per minute; one request should batch symbols.",
    ),
    ("zzshare", "security_master"): EndpointContract(
        "zzshare", "security_master", "security_master", "token", None, None,
        1.0, 1, 20.0, 2, quota_basis="operational",
    ),
    ("zzshare", "trade_calendar"): EndpointContract(
        "zzshare", "trade_calendar", "a_share_trade_calendar", "token", None, None,
        1.0, 1, 20.0, 2, supports_pit=True, quota_basis="operational",
    ),
    ("zzshare", "valuation"): EndpointContract(
        "zzshare", "valuation", "valuation_snapshot", "token", None, None,
        1.0, 1, 30.0, 2, supports_pit=True, quota_basis="operational",
    ),
    ("zzshare", "finance_pit"): EndpointContract(
        "zzshare", "finance_pit", "financial_statement_pit", "token", None, None,
        1.0, 1, 30.0, 2, supports_pit=True, quota_basis="operational",
        notes="Quarterly rows must additionally satisfy pubDate <= selection date.",
    ),
    ("eltdx", "bars"): EndpointContract(
        "eltdx", "bars", "daily/minute_ohlcv", "none", 3200, 800,
        0.05, 2, 8.0, 1, adjustment="raw", volume_unit="provider_native",
        amount_unit="provider_native", quota_basis="protocol",
        notes="800 bars per protocol page; logical request is bounded by MARKET_DATA_MAX_BARS.",
    ),
    ("eltdx", "quotes"): EndpointContract(
        "eltdx", "quotes", "realtime_quote", "none", 80, 80,
        0.05, 2, 8.0, 1, adjustment="raw", quota_basis="operational",
    ),
    ("tdx_python", "bars"): EndpointContract(
        "tdx_python", "bars", "daily/minute_ohlcv", "none", 3200, 800,
        0.05, 1, 10.0, 0, adjustment="raw", quota_basis="protocol",
        notes="Native SDK stays in a bounded worker process.",
    ),
    ("tdx_python", "quotes"): EndpointContract(
        "tdx_python", "quotes", "realtime_quote", "none", 80, 80,
        0.05, 1, 10.0, 0, adjustment="raw", quota_basis="operational",
    ),
    ("baostock", "daily"): EndpointContract(
        "baostock", "daily", "daily_ohlcv", "anonymous_login", None, None,
        0.2, 1, 15.0, 1, adjustment="raw/qfq", volume_unit="shares",
        amount_unit="yuan", quota_basis="published",
        notes="Host-wide serialized access; local hard stop defaults to 45000 requests/day.",
        daily_request_limit=50_000,
    ),
    ("baostock", "calendar"): EndpointContract(
        "baostock", "calendar", "a_share_trade_calendar", "anonymous_login", None, None,
        0.2, 1, 15.0, 1, supports_pit=True, quota_basis="published",
        notes="Independent validator; shares the host-wide 50000 request/day limit.",
        daily_request_limit=50_000,
    ),
    ("cninfo", "announcements"): EndpointContract(
        "cninfo", "announcements", "official_disclosures", "none", None, 30,
        1.0, 1, 20.0, 2, supports_pit=True, quota_basis="operational",
    ),
    ("pywencai", "discovery"): EndpointContract(
        "pywencai", "discovery", "external_reference_only", "session", None, None,
        2.0, 1, 90.0, 0, quota_basis="operational",
        notes="Reference/discovery only; never participates in the local primary score.",
    ),
}


def _register_mairui_compatible(provider: str) -> None:
    """Register the conservative free-tier boundary shared by both API products."""
    definitions = {
        "realtime": ("batch_realtime_quote", 20, False),
        "kline": ("five_minute_or_slower_ohlcv_fallback", 3200, False),
        "capital_flow": ("daily_order_flow_fallback", 365, False),
    }
    if provider == "mairui":
        definitions.update({
            "announcements": ("exchange_disclosure_reference", None, True),
            "interactive_qa": ("investor_interaction_reference", None, True),
            "limit_performance": ("limit_board_reference", 365, False),
            "auction_performance": ("auction_reference", 365, False),
        })
    for endpoint, (capability, hard_max_rows, supports_pit) in definitions.items():
        _BASE_CONTRACTS[(provider, endpoint)] = EndpointContract(
            provider, endpoint, capability, "path_token", hard_max_rows, None,
            0.25, 1, 15.0, 1, supports_pit=supports_pit,
            adjustment="raw" if endpoint == "kline" else "not_applicable",
            volume_unit="runtime_validated" if endpoint == "kline" else "not_applicable",
            amount_unit="yuan" if endpoint in {"kline", "capital_flow"} else "not_applicable",
            quota_basis="published",
            notes=("Optional fallback/reference only; one provider-wide persisted budget is "
                   "shared by all endpoints. Credentials in URL paths are never logged."),
            daily_request_limit=500,
        )


for _provider in ("mairui", "moma"):
    _register_mairui_compatible(_provider)
del _provider

# Existing atomic adapters remain usable in their actual domains, including
# unstable discovery/news sources. This does not promote them into PIT inputs.
_LEGACY_CAPABILITIES = {
    "tencent": "quotes_raw_and_qfq_bars", "sina": "quotes_bars_financial_reference",
    "eastmoney": "quotes_bars_order_flow_reference", "ths": "sector_hot_forecast_reference",
    "akshare": "last_resort_market_reference", "baidu": "news_reference",
    "cls": "news_reference", "jsl": "convertible_bond_reference",
    "eastmoney_saas": "external_reference_only", "easy_tdx": "legacy_raw_protocol",
    "mootdx": "legacy_raw_protocol", "tickflow": "optional_market_reference",
}
from data.provider_governor import PROVIDERS as _PROVIDER_POLICIES
for _provider, _capability in _LEGACY_CAPABILITIES.items():
    _tier, _interval, _ceiling = _PROVIDER_POLICIES[_provider]
    _endpoint = "discovery" if _provider == "eastmoney_saas" else "adapter"
    _BASE_CONTRACTS[(_provider, _endpoint)] = EndpointContract(
        _provider, _endpoint, _capability, "token" if _provider == "eastmoney_saas" else "none", None, None,
        _interval, 1, 15., 0, daily_request_limit=_ceiling,
        notes="Existing adapter; operational safety ceiling, not a vendor quota guarantee. Never infers PIT from live data.")
del _provider, _capability, _tier, _interval, _ceiling, _endpoint


class _Gate:
    def __init__(self, contract: EndpointContract):
        self.contract = contract
        self.semaphore = threading.BoundedSemaphore(contract.max_concurrency)
        self.lock = threading.Lock()
        self.last_call = 0.0

    @contextmanager
    def enter(self) -> Iterator[None]:
        timeout = max(self.contract.timeout_seconds, 0.1)
        if not self.semaphore.acquire(timeout=timeout):
            raise TimeoutError(
                f"source concurrency limit reached: {self.contract.provider}.{self.contract.endpoint}"
            )
        try:
            with self.lock:
                wait = self.contract.min_interval_seconds - (time.monotonic() - self.last_call)
                if wait > 0:
                    time.sleep(wait)
                self.last_call = time.monotonic()
            yield
        finally:
            self.semaphore.release()


_GATES: Dict[Tuple[str, str, EndpointContract], _Gate] = {}
_GATES_LOCK = threading.Lock()


class SourceCooldownActive(RuntimeError):
    """A safe, payload-free signal that routing must skip one endpoint."""


@contextmanager
def source_call(provider: str, endpoint: str) -> Iterator[EndpointContract]:
    """Apply the endpoint's concurrency and minimum-interval boundary."""
    contract = get_contract(provider, endpoint)
    started = time.monotonic()
    key = (provider.lower(), endpoint.lower(), contract)
    try:
        from data.runtime_capabilities import active_cooldown_until

        if active_cooldown_until(key[0], key[1]):
            raise SourceCooldownActive(
                f"source cooldown active: {key[0]}.{key[1]}"
            )
    except ImportError:
        pass
    with _GATES_LOCK:
        gate = _GATES.setdefault(key, _Gate(contract))
    rate_slot = None
    # Do not probe an implicit localhost Redis from data-source hot paths. A
    # shared gate is enabled only when deployment explicitly configures Redis.
    if os.getenv("REDIS_URL") or os.getenv("REDIS_HOST"):
        try:
            from cache import rate_slot
        except Exception:
            rate_slot = None
    try:
        from contextlib import nullcontext
        from data.provider_governor import provider_slot
        # SDK calls which bypass the shared HTTP session still receive a host-wide
        # guard. Baostock and path-token APIs already own stricter native budgets.
        shared = (nullcontext() if provider in {"baostock", "mairui", "moma"}
                  else provider_slot(provider, wait_seconds=contract.timeout_seconds,
                                     interval=contract.min_interval_seconds))
        from data.acquisition_evidence import source_family
        family = source_family(provider)
        family_slot = (provider_slot(family, wait_seconds=contract.timeout_seconds)
                       if family not in {provider, "unknown"} else nullcontext())
        with gate.enter(), family_slot, shared:
            if rate_slot is None:
                yield contract
            else:
                with rate_slot(
                    f"source:{key[0]}:{key[1]}",
                    contract.min_interval_seconds,
                    contract.timeout_seconds,
                ):
                    yield contract
    except Exception as exc:
        from data.acquisition_evidence import receipt
        receipt(provider, endpoint, started, error=exc)
        try:
            from data.runtime_capabilities import record_failure

            record_failure(key[0], key[1], exc)
        except Exception:
            pass
        raise
    else:
        from data.acquisition_evidence import receipt
        receipt(provider, endpoint, started)
        try:
            from data.runtime_capabilities import record_success

            record_success(key[0], key[1])
        except Exception:
            pass


def _reset_for_tests() -> None:
    with _GATES_LOCK:
        _GATES.clear()
