"""Safe cross-process runtime state for market-data endpoints.

Only bounded operational metadata is stored.  Credentials, URLs, request arguments, provider
messages and returned market/financial payloads are deliberately absent.
"""

from __future__ import annotations

import os
import queue
import threading
from datetime import datetime, timedelta
from typing import Any


_LOCK = threading.Lock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}
_PERSIST_QUEUE: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(maxsize=256)
_PERSIST_WORKER: threading.Thread | None = None
_PERSIST_WORKER_LOCK = threading.Lock()

DATASET_ROUTES: dict[str, dict[str, Any]] = {
    "security_master": {"primary": "zzshare.security_master", "fallback": []},
    "trade_calendar": {
        "primary": "zzshare.trade_calendar", "validators": ["baostock.calendar"],
        "policy": "two_provider_consensus",
    },
    "daily_qfq": {
        "primary": "zzshare.daily_market", "fallback": ["baostock.daily"],
        "policy": "baostock_exact_date_repair_only",
    },
    "daily_raw_validation": {
        "primary": "eltdx.bars", "fallback": ["tdx_python.bars", "baostock.daily"],
        "policy": "never_repair_qfq",
    },
    "minute": {
        "primary": "zzshare.minute",
        "fallback": ["eltdx.bars", "tdx_python.bars", "mairui.kline", "moma.kline"],
        "policy": "mairui_compatible_sources_support_5m_plus_without_pro",
    },
    "realtime_quote": {
        "primary": "zzshare.realtime",
        "fallback": ["eltdx.quotes", "tdx_python.quotes", "mairui.realtime", "moma.realtime"],
    },
    "valuation": {"primary": "zzshare.valuation", "fallback": []},
    "financial_pit": {"primary": "zzshare.finance_pit", "fallback": []},
    "official_disclosure": {
        "primary": "cninfo.announcements",
        "fallback": ["mairui.announcements"],
        "policy": "aggregator_fallback_is_not_marked_official",
    },
    "daily_order_flow": {
        "primary": "eastmoney",
        "fallback": ["mairui.capital_flow", "moma.capital_flow"],
    },
    "market_microstructure_reference": {
        "primary": "mairui.limit_performance",
        "fallback": [],
        "policy": "reference_only",
    },
    "investor_interaction_reference": {
        "primary": "mairui.interactive_qa",
        "fallback": [],
        "policy": "reference_only",
    },
    "external_reference": {
        "primary": "pywencai.discovery", "fallback": [], "policy": "reference_only",
    },
}


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _configured(provider: str, auth: str) -> bool:
    if auth in {"none", "anonymous_login"}:
        return True
    names = {
        "zzshare": ("ZZSHARE_TOKEN",),
        "pywencai": ("PYWENCAI_COOKIE", "WENCAI_COOKIE"),
        "mairui": ("MAIRUI_LICENCE",),
        "moma": ("MOMA_TOKEN",),
    }.get(provider, ())
    return any(str(os.getenv(name) or "").strip() for name in names)


def _enabled(provider: str) -> bool:
    flags = {
        "zzshare": "ZZSHARE_ENABLED",
        "eltdx": "TDX_USE_ELTDX",
        "tdx_python": "TDX_USE_TDX_PYTHON",
        "mairui": "MAIRUI_ENABLED",
        "moma": "MOMA_ENABLED",
    }
    name = flags.get(provider)
    if not name:
        return True
    return str(os.getenv(name, "true")).lower() not in {"0", "false", "no", "off"}


def _connect():
    from db_compat import connect

    return connect("runtime_capabilities")


def ensure_schema() -> None:
    conn = _connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS research_source_runtime_state (
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                last_success_at TEXT,
                last_failure_at TEXT,
                last_error_category TEXT,
                cooldown_until TEXT,
                freshness_as_of TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider, endpoint)
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def _persist(provider: str, endpoint: str, state: dict[str, Any]) -> None:
    try:
        ensure_schema()
        conn = _connect()
        try:
            conn.execute(
                """INSERT INTO research_source_runtime_state
                   (provider,endpoint,last_success_at,last_failure_at,last_error_category,
                    cooldown_until,freshness_as_of,updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider,endpoint) DO UPDATE SET
                     last_success_at=excluded.last_success_at,
                     last_failure_at=excluded.last_failure_at,
                     last_error_category=excluded.last_error_category,
                     cooldown_until=excluded.cooldown_until,
                     freshness_as_of=excluded.freshness_as_of,
                     updated_at=excluded.updated_at""",
                (provider, endpoint, state.get("last_success_at"), state.get("last_failure_at"),
                 state.get("last_error_category"), state.get("cooldown_until"),
                 state.get("freshness_as_of"), state["updated_at"]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # Data access must not fail merely because diagnostic persistence is unavailable.
        return


def _persist_worker() -> None:
    while True:
        provider, endpoint, state = _PERSIST_QUEUE.get()
        try:
            _persist(provider, endpoint, state)
        finally:
            _PERSIST_QUEUE.task_done()


def _schedule_persist(provider: str, endpoint: str, state: dict[str, Any]) -> None:
    """Persist best-effort diagnostics without delaying provider calls.

    The queue is deliberately bounded.  Dropping an operational-state sample is safer than
    applying back-pressure to market-data or research work; the latest in-memory state remains
    available to the current process either way.
    """
    global _PERSIST_WORKER
    with _PERSIST_WORKER_LOCK:
        if _PERSIST_WORKER is None or not _PERSIST_WORKER.is_alive():
            _PERSIST_WORKER = threading.Thread(
                target=_persist_worker,
                name="foliant-runtime-state",
                daemon=True,
            )
            _PERSIST_WORKER.start()
    try:
        _PERSIST_QUEUE.put_nowait((provider, endpoint, dict(state)))
    except queue.Full:
        return


def record_success(provider: str, endpoint: str, *, freshness_as_of: str | None = None) -> None:
    key = (str(provider).lower(), str(endpoint).lower())
    with _LOCK:
        state = dict(_MEMORY.get(key) or {})
        state.update({
            "last_success_at": _now_iso(),
            "last_error_category": None,
            "cooldown_until": None,
            "updated_at": _now_iso(),
        })
        if freshness_as_of:
            state["freshness_as_of"] = str(freshness_as_of)[:40]
        _MEMORY[key] = state
    _schedule_persist(key[0], key[1], state)


def record_failure(provider: str, endpoint: str, error: BaseException,
                   *, cooldown_seconds: int | None = None) -> None:
    key = (str(provider).lower(), str(endpoint).lower())
    category = type(error).__name__[:80]
    if cooldown_seconds is None and any(
        token in category.lower() for token in ("quota", "rate", "budget", "throttle")
    ):
        cooldown_seconds = 60
    now = _now()
    with _LOCK:
        state = dict(_MEMORY.get(key) or {})
        state.update({
            "last_failure_at": now.isoformat(timespec="seconds"),
            "last_error_category": category,
            "cooldown_until": (
                (now + timedelta(seconds=max(1, min(3600, int(cooldown_seconds))))).isoformat(
                    timespec="seconds"
                ) if cooldown_seconds else None
            ),
            "updated_at": now.isoformat(timespec="seconds"),
        })
        _MEMORY[key] = state
    _schedule_persist(key[0], key[1], state)


def active_cooldown_until(provider: str, endpoint: str) -> str | None:
    """Return an active process-local cooldown without performing database I/O."""
    key = (str(provider).lower(), str(endpoint).lower())
    with _LOCK:
        value = str((_MEMORY.get(key) or {}).get("cooldown_until") or "")
    if not value:
        return None
    try:
        return value if datetime.fromisoformat(value) > _now() else None
    except (TypeError, ValueError):
        return None


def _stored_states() -> dict[tuple[str, str], dict[str, Any]]:
    values: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        ensure_schema()
        conn = _connect()
        try:
            rows = conn.execute(
                """SELECT provider,endpoint,last_success_at,last_failure_at,
                          last_error_category,cooldown_until,freshness_as_of,updated_at
                   FROM research_source_runtime_state"""
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            values[(str(row[0]), str(row[1]))] = {
                "last_success_at": row[2], "last_failure_at": row[3],
                "last_error_category": row[4], "cooldown_until": row[5],
                "freshness_as_of": row[6], "updated_at": row[7],
            }
    except Exception:
        pass
    with _LOCK:
        for key, state in _MEMORY.items():
            if not values.get(key) or str(state.get("updated_at") or "") >= str(
                values[key].get("updated_at") or ""
            ):
                values[key] = dict(state)
    return values


def capability_snapshot() -> dict[str, Any]:
    from data.source_contracts import contracts

    runtime = _stored_states()
    providers: dict[str, dict[str, Any]] = {}
    for provider, endpoints in contracts().items():
        provider_out: dict[str, Any] = {}
        for endpoint, contract in endpoints.items():
            state = dict(runtime.get((provider, endpoint)) or {})
            configured = _configured(provider, str(contract.get("auth") or "none"))
            enabled = _enabled(provider)
            try:
                cooldown_active = bool(
                    state.get("cooldown_until")
                    and datetime.fromisoformat(str(state["cooldown_until"])) > _now()
                )
            except (TypeError, ValueError):
                cooldown_active = False
            item = {
                "configured": configured,
                "enabled": enabled,
                "available": bool(configured and enabled and not cooldown_active),
                "capability": contract.get("capability"),
                "last_success_at": state.get("last_success_at"),
                "last_failure_at": state.get("last_failure_at"),
                "last_error_category": state.get("last_error_category"),
                "cooldown_until": state.get("cooldown_until"),
                "freshness_as_of": state.get("freshness_as_of"),
                "limits": {
                    "hard_max_rows": contract.get("hard_max_rows"),
                    "page_size": contract.get("page_size"),
                    "min_interval_seconds": contract.get("min_interval_seconds"),
                    "max_concurrency": contract.get("max_concurrency"),
                    "timeout_seconds": contract.get("timeout_seconds"),
                    "retries": contract.get("retries"),
                    "daily_request_limit": contract.get("daily_request_limit"),
                },
            }
            provider_out[endpoint] = item
        providers[provider] = provider_out
    try:
        from data.sources.baostock import request_budget_status, runtime_status

        budget = request_budget_status()
        for endpoint in ("daily", "calendar"):
            if endpoint in providers.get("baostock", {}):
                item = providers["baostock"][endpoint]
                breaker = runtime_status(endpoint)
                item["quota"] = budget
                item["cooldown_until"] = breaker.get("cooldown_until") or item.get(
                    "cooldown_until"
                )
                item["last_error_category"] = breaker.get(
                    "last_error_category"
                ) or item.get("last_error_category")
                item["available"] = bool(
                    item.get("available")
                    and budget.get("available")
                    and breaker.get("available")
                )
    except Exception:
        pass
    for provider in ("mairui", "moma"):
        try:
            module = __import__(f"data.sources.{provider}", fromlist=["request_budget_status"])
            budget = module.request_budget_status()
            for item in providers.get(provider, {}).values():
                item["quota"] = budget
                item["available"] = bool(
                    item.get("available") and budget.get("available")
                )
        except Exception:
            pass
    return {
        "schema_version": "runtime-data-capabilities-v1",
        "generated_at": _now_iso(),
        "dataset_routes": DATASET_ROUTES,
        "providers": providers,
    }


def _reset_for_tests() -> None:
    with _LOCK:
        _MEMORY.clear()
