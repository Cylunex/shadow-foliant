"""Shared safe transport for HTTPS APIs that place credentials in URL paths.

The transport intentionally never logs or raises a message containing the request URL,
query parameters, response body, or credential.  It also persists a conservative daily
budget across the Web, jobs and Agent processes running on the same host.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, Iterator, Optional
from urllib.parse import quote, urlencode, urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses msvcrt below.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux production uses fcntl above.
    msvcrt = None

from data.source_contracts import get_contract, source_call


class ProviderBudgetExceeded(RuntimeError):
    """Raised before an HTTP call would exceed the local daily safety budget."""


class ProviderRequestFailed(RuntimeError):
    """Payload-free provider failure safe for runtime diagnostics."""


class PathTokenApi:
    def __init__(self, *, provider: str, token_env: str, enabled_env: str,
                 base_url_env: str, default_base_url: str,
                 state_dir_env: str, budget_env: str,
                 published_daily_limit: int = 500,
                 default_daily_budget: int = 450):
        self.provider = provider
        self.token_env = token_env
        self.enabled_env = enabled_env
        self.base_url_env = base_url_env
        self.default_base_url = default_base_url.rstrip("/")
        self.state_dir_env = state_dir_env
        self.budget_env = budget_env
        self.published_daily_limit = max(1, int(published_daily_limit))
        self.default_daily_budget = min(
            self.published_daily_limit, max(1, int(default_daily_budget))
        )
        self._lock = threading.RLock()
        self._session = None

    def enabled(self) -> bool:
        return str(os.getenv(self.enabled_env, "true")).lower() not in {
            "0", "false", "no", "off",
        }

    def available(self) -> bool:
        return bool(self.enabled() and str(os.getenv(self.token_env) or "").strip())

    def _token(self) -> str:
        return str(os.getenv(self.token_env) or "").strip()

    def _base_url(self) -> str:
        value = str(os.getenv(self.base_url_env) or self.default_base_url).strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise ProviderRequestFailed(f"{self.provider} requires https base url")
        return value

    def _daily_budget(self) -> int:
        try:
            configured = int(os.getenv(self.budget_env, str(self.default_daily_budget)))
        except (TypeError, ValueError):
            configured = self.default_daily_budget
        return min(self.published_daily_limit, max(1, configured))

    def _state_path(self) -> Path:
        configured = str(os.getenv(self.state_dir_env) or "").strip()
        if configured:
            root = Path(configured).expanduser()
        else:
            import _bootstrap
            root = Path(_bootstrap.DB_DIR) / "provider_state"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{self.provider}-guard.json"

    @staticmethod
    def _read_state(handle) -> dict:
        handle.seek(0)
        try:
            state = json.loads(handle.read() or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}
        today = date.today().isoformat()
        if state.get("date") != today:
            state = {"date": today, "count": 0, "capabilities": {}}
        try:
            state["count"] = max(0, int(state.get("count") or 0))
        except (TypeError, ValueError):
            state["count"] = 0
        if not isinstance(state.get("capabilities"), dict):
            state["capabilities"] = {}
        return state

    @staticmethod
    def _write_state(handle, state: dict) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        handle.flush()

    @contextmanager
    def _budget_slot(self, timeout: float = 2.0) -> Iterator[Optional[Any]]:
        if not self._lock.acquire(timeout=max(0.01, float(timeout))):
            yield None
            return
        handle = None
        lock_handle = None
        locked = False
        try:
            state_path = self._state_path()
            handle = state_path.open("a+", encoding="utf-8")
            lock_handle = state_path.with_suffix(".lock").open("a+b")
            deadline = time.monotonic() + max(0.01, float(timeout))
            if fcntl is not None:
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
            elif msvcrt is not None:
                lock_handle.seek(0, os.SEEK_END)
                if lock_handle.tell() == 0:
                    lock_handle.write(b"\0")
                    lock_handle.flush()
                while time.monotonic() < deadline:
                    try:
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.05)
            yield handle if locked else None
        finally:
            if lock_handle is not None:
                if locked and fcntl is not None:
                    try:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                elif locked and msvcrt is not None:
                    try:
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                lock_handle.close()
            if handle is not None:
                handle.close()
            self._lock.release()

    def _reserve(self, capability: str) -> None:
        with self._budget_slot() as handle:
            if handle is None:
                raise ProviderRequestFailed(f"{self.provider} budget state busy")
            state = self._read_state(handle)
            limit = self._daily_budget()
            if state["count"] >= limit:
                raise ProviderBudgetExceeded(
                    f"{self.provider} daily request budget exhausted"
                )
            state["count"] += 1
            counts = state["capabilities"]
            counts[str(capability)] = max(0, int(counts.get(str(capability)) or 0)) + 1
            state["last_capability"] = str(capability)
            state["last_request_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self._write_state(handle, state)

    def budget_status(self) -> dict:
        with self._budget_slot() as handle:
            if handle is None:
                return {"available": False, "reason": "provider_busy"}
            state = self._read_state(handle)
            limit = self._daily_budget()
            return {
                "available": state["count"] < limit,
                "date": state["date"],
                "used": state["count"],
                "limit": limit,
                "remaining": max(0, limit - state["count"]),
                "capabilities": dict(state.get("capabilities") or {}),
            }

    def _get_session(self):
        with self._lock:
            if self._session is None:
                import requests
                session = requests.Session()
                # Production calls leave through the NAS only.  Ignoring ambient proxy
                # variables prevents a credential-bearing URL crossing an unintended proxy.
                session.trust_env = False
                session.headers.update({
                    "Accept": "application/json",
                    "User-Agent": "Shadow-Foliant/provider-client",
                })
                self._session = session
            return self._session

    @staticmethod
    def rows(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if not isinstance(payload, dict):
            return []
        data = payload.get("data", payload.get("result", payload.get("items")))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            for key in ("list", "rows", "items", "data"):
                rows = data.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            return [data]
        # Raw dictionary records are accepted only when they look like market rows,
        # not when they are an API error envelope.
        if not any(key in payload for key in ("code", "msg", "message", "error")):
            return [payload]
        return []

    def get(self, capability: str, endpoint_parts: list[str],
            params: Optional[Dict[str, Any]] = None) -> list[dict]:
        if not self.available():
            return []
        token = self._token()
        if not token:
            return []
        contract = get_contract(self.provider, capability)
        safe_parts = [quote(str(part).strip(), safe="._-") for part in endpoint_parts]
        safe_parts.append(quote(token, safe=""))
        query = {
            str(key): value for key, value in (params or {}).items()
            if value is not None and str(value) != ""
        }
        for attempt in range(contract.retries + 1):
            try:
                url = f"{self._base_url()}/{'/'.join(safe_parts)}"
                if query:
                    url += "?" + urlencode(query)
                with source_call(self.provider, capability):
                    # Reserve only after the runtime cooldown gate admits the call.
                    self._reserve(capability)
                    response = self._get_session().get(
                        url, timeout=contract.timeout_seconds
                    )
                    if int(response.status_code) != 200:
                        raise ProviderRequestFailed(
                            f"{self.provider} http status {int(response.status_code)}"
                        )
                    try:
                        payload = response.json()
                    except Exception as exc:
                        raise ProviderRequestFailed(
                            f"{self.provider} invalid json response"
                        ) from exc
                    if isinstance(payload, dict):
                        code = payload.get("code")
                        if code not in (None, 0, 200, "0", "200", "success"):
                            raise ProviderRequestFailed(
                                f"{self.provider} api error code {str(code)[:16]}"
                            )
                return self.rows(payload)
            except ProviderBudgetExceeded:
                return []
            except Exception:
                if attempt >= contract.retries:
                    return []
                time.sleep(min(2 ** attempt, 2))
        return []

    def reset_for_tests(self) -> None:
        with self._lock:
            self._session = None
