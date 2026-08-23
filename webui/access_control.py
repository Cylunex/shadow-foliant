"""Explicit, default-deny route authorization for the Foliant WebUI."""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Match

from webui.platform_auth import SESSION_COOKIE, WebAuthError, get_web_auth_service


_STATIC_ROOT = Path(__file__).with_name("static").resolve()


class Access(str, Enum):
    PUBLIC = "public"
    READY = "ready"
    USER = "user"
    ADMIN = "admin"
    MACHINE = "machine"


def _add(target: dict[tuple[str, str], Access], access: Access, method: str, *paths: str) -> None:
    for path in paths:
        target[(method, path)] = access


ROUTE_POLICIES: dict[tuple[str, str], Access] = {}

_add(ROUTE_POLICIES, Access.PUBLIC, "GET", "/healthz", "/api/health", "/auth/login", "/auth/callback")
_add(ROUTE_POLICIES, Access.READY, "GET", "/readyz", "/research-readyz",
     "/data-readyz", "/selection-readyz")
_add(ROUTE_POLICIES, Access.USER, "GET", "/api/auth/me")
_add(ROUTE_POLICIES, Access.USER, "POST", "/api/auth/session/rotate", "/auth/logout", "/auth/logout/all")

_add(
    ROUTE_POLICIES,
    Access.USER,
    "GET",
    "/api/stock/{code}",
    "/api/stock/{code}/kline",
    "/api/stock/{code}/insights",
    "/api/stock/{code}/dcf",
    "/api/stock/{code}/backtest",
    "/api/stock/{code}/deep-analysis/history",
    "/api/deep-analysis/history/all",
    "/api/fund/{code}/nav",
    "/api/fund/{code}/score",
    "/api/fund/screen",
    "/api/fund/valuation",
    "/api/fund/compare",
    "/api/fund/{code}/extras",
    "/api/fund/{code}",
    "/api/factors/eval",
    "/api/factors/pv-screen",
    "/api/market/trade-dates",
    "/api/market/dragon",
    "/api/market/lhb-inst",
    "/api/market/lhb-ai",
    "/api/market/hot",
    "/api/market/indices",
    "/api/market/news",
    "/api/market/news-sentiment",
    "/api/market/news-ai",
    "/api/market/news/{code}",
    "/api/market/north",
    "/api/screen/multifactor",
    "/api/sector/board",
    "/api/sector/ai-rotation",
    "/api/macro",
    "/api/rag/search",
    "/api/miaoxiang",
    "/api/rag/stats",
    "/api/history/eval",
    "/api/history/eval/by",
    "/api/research/industry-reports",
    "/api/signals/outcomes/stats",
    "/api/screen/latest",
    "/api/research/source-contracts",
    "/api/convertible/screen",
    "/api/screen/strategy/{name}",
    "/api/strategy-genome/scores",
    "/api/strategy-genome/deployment",
    "/api/strategy-genome/variants",
    "/api/strategy-genome/affinity",
    "/api/strategy-genome/ab",
    "/api/strategy-genome/scores/history",
)
_add(
    ROUTE_POLICIES,
    Access.USER,
    "POST",
    "/api/backtest/portfolio",
    "/api/stock/{code}/deep-analysis",
    "/api/fund/dca-backtest",
    "/api/fund/dca-compare",
    "/api/fund/{code}/ai-panel",
    "/api/screen/recipe",
)

_add(
    ROUTE_POLICIES,
    Access.ADMIN,
    "GET",
    "/api/monitor/stocks",
    "/api/monitor/notifications",
    "/api/fund/holdings",
    "/api/fund/transactions",
    "/api/fund/plans",
    "/api/fund/diagnose",
    "/api/fund/combined-view",
    "/api/portfolio/overview",
    "/api/portfolio/daily-pnl",
    "/api/portfolio/stocks",
    "/api/portfolio/stress",
    "/api/portfolio/curve",
    "/api/portfolio/trade-records",
    "/api/portfolio/performance",
    "/api/portfolio/optimize",
    "/api/portfolio/benchmark",
    "/api/portfolio/montecarlo",
    "/api/portfolio/xray",
    "/api/trades/behavior",
    "/api/trades/realized",
    "/api/portfolio/signals",
    "/api/portfolio/insights",
    "/api/portfolio/classify",
    "/api/portfolio/diagnose-ai",
    "/api/workflow/providers",
    "/api/workflow/list",
    "/api/workflow/preview-data",
    "/api/workflow/runs",
    "/api/workflow/run/{run_id}",
    "/api/briefing/morning",
    "/api/portfolio/exit-advice",
    "/api/llm/usage",
    "/api/signals",
    "/api/signals/latest/{code}",
    "/api/jobs",
    "/api/jobs/{name}/runs",
    "/api/task-runs",
    "/api/task-runs/{run_id}",
    "/api/agent/cockpit",
    "/api/env",
)
_add(
    ROUTE_POLICIES,
    Access.ADMIN,
    "POST",
    "/api/monitor/stocks",
    "/api/fund/nav-refresh",
    "/api/fund/transaction",
    "/api/fund/plan",
    "/api/fund/transactions/import",
    "/api/fund/plans/import",
    "/api/fund/plan/{plan_id}/toggle",
    "/api/portfolio/snapshot",
    "/api/portfolio/trade-records/preview",
    "/api/portfolio/trade-records",
    "/api/market/north-refresh",
    "/api/reco/dual-horizon",
    "/api/workflow/save",
    "/api/workflow/run",
    "/api/briefing/ai-summary",
    "/api/signals/{signal_id}/status",
    "/api/signals/outcomes/run",
    "/api/jobs/{name}/toggle",
    "/api/jobs/{name}/run",
    "/api/env",
)
_add(
    ROUTE_POLICIES,
    Access.ADMIN,
    "DELETE",
    "/api/monitor/stocks/{code}",
    "/api/fund/holdings/{code}",
    "/api/fund/plan/{plan_id}",
    "/api/workflow/{wid}",
)

_add(
    ROUTE_POLICIES,
    Access.MACHINE,
    "GET",
    "/api/machine/runtime-health",
    "/api/machine/agent/cockpit",
    "/api/machine/research/{code}",
    "/api/machine/v1/agent/market/overview",
    "/api/machine/v1/agent/market/data-quality",
    "/api/machine/v1/agent/securities/{symbol}/research/latest",
    "/api/machine/v1/agent/selection-runs/latest",
    "/api/machine/v1/agent/runs/{run_id}",
    "/api/machine/v1/agent/runs/{run_id}/result",
)

_add(
    ROUTE_POLICIES,
    Access.MACHINE,
    "POST",
    "/api/machine/v1/agent/securities/{symbol}/research-runs",
    "/api/machine/v1/agent/selection-runs",
    "/api/machine/v1/agent/backtest-runs",
)

MACHINE_SCOPES = {
    ("GET", "/api/machine/runtime-health"): "stock.read",
    ("GET", "/api/machine/agent/cockpit"): "stock.read",
    ("GET", "/api/machine/research/{code}"): "stock.research",
    ("GET", "/api/machine/v1/agent/market/overview"): "stock.read",
    ("GET", "/api/machine/v1/agent/market/data-quality"): "stock.read",
    ("GET", "/api/machine/v1/agent/securities/{symbol}/research/latest"): "stock.research",
    ("POST", "/api/machine/v1/agent/securities/{symbol}/research-runs"): "stock.research",
    ("GET", "/api/machine/v1/agent/selection-runs/latest"): "stock.research",
    ("POST", "/api/machine/v1/agent/selection-runs"): "stock.research",
    ("POST", "/api/machine/v1/agent/backtest-runs"): "stock.research",
    ("GET", "/api/machine/v1/agent/runs/{run_id}"): "stock.research",
    ("GET", "/api/machine/v1/agent/runs/{run_id}/result"): "stock.research",
}

MACHINE_CAPABILITIES = {
    ("GET", "/api/machine/v1/agent/market/overview"): "foliant.market.read",
    ("GET", "/api/machine/v1/agent/market/data-quality"): "foliant.market.read",
    ("GET", "/api/machine/v1/agent/securities/{symbol}/research/latest"):
        "foliant.security-research.read",
    ("POST", "/api/machine/v1/agent/securities/{symbol}/research-runs"):
        "foliant.security-research.preview",
    ("GET", "/api/machine/v1/agent/selection-runs/latest"): "foliant.selection.read",
    ("POST", "/api/machine/v1/agent/selection-runs"): "foliant.selection.preview",
    ("POST", "/api/machine/v1/agent/backtest-runs"): "foliant.backtest.preview",
    ("GET", "/api/machine/v1/agent/runs/{run_id}"): "foliant.run.read",
    ("GET", "/api/machine/v1/agent/runs/{run_id}/result"): "foliant.run.read",
}

_audit_logger = logging.getLogger("webui.security_audit")
_agent_authenticator: Any = None
_agent_auth_lock = threading.Lock()


def route_key_for_request(request: Request) -> tuple[str, str] | None:
    method = request.method.upper()
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is Match.FULL:
            path = getattr(route, "path", None)
            if path is not None:
                return method, path
    return None


def unclassified_routes(app: Any) -> list[tuple[str, str]]:
    missing = []
    for route in app.router.routes:
        path = getattr(route, "path", "")
        if not path or path in {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}:
            continue
        for method in sorted(getattr(route, "methods", set()) or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, path) not in ROUTE_POLICIES:
                missing.append((method, path))
    return missing


async def enforce_request_access(request: Request) -> Response | None:
    key = route_key_for_request(request)
    path = request.url.path
    if key is None or (key[1] == "" and not path.startswith(("/api", "/auth"))):
        if path.startswith("/api") or path.startswith("/auth"):
            return _json_error(403, "route is not authorized")
        # StaticFiles 挂载在根路径，会让任意扫描路径都匹配成 SPA。旧逻辑先将这些
        # 不存在的路径重定向到 OIDC，扫描器跟随后会创建登录事务并访问 IdP。
        # 只允许真实入口和已存在的静态文件进入浏览器鉴权，其余直接 404。
        if not _is_known_spa_resource(path):
            return _json_error(404, "not found")
        access = Access.USER
        template = "<spa>"
    else:
        access = ROUTE_POLICIES.get(key)
        template = key[1]
        if access is None:
            return _json_error(403, "route is not authorized")

    request.state.route_policy = access.value
    request.state.route_template = template
    request.state.request_id = _request_id(request)

    if access is Access.PUBLIC:
        return None
    if access is Access.READY:
        return _authenticate_machine(request, "stock.read")
    if access is Access.MACHINE:
        return _authenticate_machine(
            request, MACHINE_SCOPES[key], MACHINE_CAPABILITIES.get(key)
        )

    try:
        service = get_web_auth_service()
    except WebAuthError:
        return _json_error(503, "browser authentication is unavailable")
    token = request.cookies.get(SESSION_COOKIE, "")
    record = service.store.authenticate_session(token)
    if record is None:
        if path.startswith("/api"):
            return _json_error(401, "authentication required")
        relative = path
        if request.url.query:
            relative += "?" + request.url.query
        return RedirectResponse(
            url=f"/auth/login?return_to={quote(relative, safe='')}", status_code=302
        )
    request.state.browser_session = record
    request.state.browser_identity = record.identity
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(
        request, service.config.redirect_uri
    ):
        return _json_error(403, "same-origin request required")
    if access is Access.ADMIN and not record.identity.in_group(service.config.admin_group):
        return _json_error(403, "administrator permission required")
    return None


def audit_request(request: Request, response: Response) -> None:
    policy = getattr(request.state, "route_policy", "")
    privileged = policy == Access.MACHINE.value or (
        policy == Access.ADMIN.value and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    )
    if not privileged:
        return
    identity = getattr(request.state, "browser_identity", None)
    agent = getattr(request.state, "agent_identity", None)
    event = {
        "event": "privileged_request",
        "request_id": getattr(request.state, "request_id", ""),
        "method": request.method.upper(),
        "route": getattr(request.state, "route_template", ""),
        "policy": policy,
        "actor_type": "agent" if policy == Access.MACHINE.value else "user",
        "actor_id": getattr(agent, "agent_id", "") or getattr(identity, "shadow_user_id", ""),
        "owner_app": getattr(agent, "owner_app", ""),
        "audience": getattr(agent, "audience", ""),
        "scope": getattr(request.state, "required_scope", ""),
        "capability": getattr(request.state, "required_capability", ""),
        "result": "allowed" if response.status_code < 400 else "rejected",
        "status_code": int(response.status_code),
    }
    _audit_logger.info(json.dumps(event, ensure_ascii=True, separators=(",", ":")))


def reset_agent_authenticator() -> None:
    global _agent_authenticator
    _agent_authenticator = None


def _authenticate_machine(request: Request, required_scope: str,
                          required_capability: str | None = None) -> Response | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization:
        return _json_error(401, "agent Bearer required")
    try:
        authenticator = _get_agent_authenticator()
        identity = authenticator.authenticate(authorization)
    except Exception:
        return _json_error(401, "invalid agent Bearer")
    if required_scope not in identity.scopes:
        return _json_error(403, "agent scope is insufficient")
    if required_capability and required_capability not in getattr(
        identity, "capabilities", frozenset()
    ):
        return _json_error(403, "agent capability is insufficient")
    request.state.agent_identity = identity
    request.state.required_scope = required_scope
    request.state.required_capability = required_capability or ""
    return None


def _get_agent_authenticator() -> Any:
    global _agent_authenticator
    if _agent_authenticator is not None:
        return _agent_authenticator
    with _agent_auth_lock:
        if _agent_authenticator is None:
            try:
                from shadow_sdk.agent import AgentAuthenticator
            except ImportError as exc:
                raise RuntimeError("Shadow Agent SDK is unavailable") from exc
            registry = os.getenv("SHADOW_AGENT_REGISTRY_FILE", "").strip()
            secrets_dir = os.getenv("SHADOW_PLATFORM_SECRETS_DIR", "").strip()
            if not registry or not secrets_dir:
                raise RuntimeError("Shadow Agent registry is not configured")
            _agent_authenticator = AgentAuthenticator(
                registry, secrets_dir=secrets_dir, audience="foliant"
            )
    return _agent_authenticator


def _json_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _is_known_spa_resource(path: str) -> bool:
    if path in {"/", "/index.html"}:
        return True
    relative = unquote(str(path or "")).lstrip("/")
    if not relative or "\\" in relative:
        return False
    try:
        candidate = (_STATIC_ROOT / relative).resolve()
        candidate.relative_to(_STATIC_ROOT)
    except (OSError, ValueError):
        return False
    return candidate.is_file()


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _same_origin(request: Request, canonical_url: str) -> bool:
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return False
    expected = urlsplit(canonical_url)
    actual = urlsplit(origin)
    expected_port = expected.port or (443 if expected.scheme == "https" else 80)
    actual_port = actual.port or (443 if actual.scheme == "https" else 80)
    return (
        actual.scheme == expected.scheme
        and actual.hostname == expected.hostname
        and actual_port == expected_port
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "").strip()
    if supplied and len(supplied) <= 128 and all(char.isalnum() or char in "-_." for char in supplied):
        return supplied
    return secrets.token_hex(12)
