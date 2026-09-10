"""Fetch one bounded Foliant snapshot for cron/Codex heartbeats.

Configuration is repository-external. The command never connects to PostgreSQL and
defaults to no notification.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


ENDPOINT = "/api/machine/v1/agent/scheduled-snapshot"
SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+\S+|postgres(?:ql)?://\S+|https?://\S+|(?:token|secret|password|cookie)\s*[:=]\s*\S+)"
)


def _failure(code: str, hint: str, *, status: str = "missing") -> dict[str, Any]:
    return {
        "schema_version": "scheduled-agent-snapshot-v1",
        "status": status,
        "error": {"code": code, "repair_hint": hint},
        "notification": {"requested": False, "sent": False},
    }


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        return SECRET_PATTERN.sub("[redacted]", value)
    return value


def _load_token() -> str:
    token_file = os.getenv("FOLIANT_AGENT_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text("utf-8").strip()
        except (OSError, UnicodeError):
            return ""
    return os.getenv("FOLIANT_AGENT_TOKEN", "").strip()


def _configured_client():
    base_url = os.getenv("FOLIANT_AGENT_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None, _failure(
            "agent_base_url_missing", "Set FOLIANT_AGENT_BASE_URL outside the repository.",
        )
    parsed = urlsplit(base_url)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    allow_http = os.getenv("FOLIANT_AGENT_ALLOW_HTTP", "").lower() == "true"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and (loopback or allow_http)):
        return None, _failure(
            "agent_transport_insecure",
            "Use HTTPS, loopback HTTP, or explicitly set FOLIANT_AGENT_ALLOW_HTTP=true.",
        )
    token = _load_token()
    if not token:
        return None, _failure(
            "agent_token_missing",
            "Set FOLIANT_AGENT_TOKEN_FILE (preferred) or FOLIANT_AGENT_TOKEN outside the repository.",
        )
    try:
        timeout = min(60.0, max(1.0, float(os.getenv("FOLIANT_AGENT_TIMEOUT_SECONDS", "30"))))
    except ValueError:
        timeout = 30.0
    return (base_url + ENDPOINT, token, timeout), None


def fetch_snapshot() -> dict[str, Any]:
    client, failure = _configured_client()
    if failure:
        return failure
    url, token, timeout = client
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return _failure(
            "agent_unreachable",
            "Verify FOLIANT_AGENT_BASE_URL, TLS trust, network reachability, and the Foliant service.",
            status="degraded",
        )
    if response.status_code in {401, 403}:
        return _failure(
            "agent_authorization_failed",
            "Provision the scheduled Agent with stock.portfolio.read, foliant.scheduled-report.read, and portfolio-primary grant.",
        )
    if response.status_code != 200:
        return _failure(
            "agent_service_failed", "Inspect the protected Foliant service logs by request time.",
            status="degraded",
        )
    try:
        payload = response.json()
    except ValueError:
        return _failure(
            "agent_response_invalid", "Verify the configured endpoint is the Foliant Agent API.",
            status="degraded",
        )
    snapshot = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "scheduled-agent-snapshot-v1":
        return _failure(
            "agent_contract_mismatch", "Deploy a Foliant version exposing scheduled-agent-snapshot-v1.",
            status="degraded",
        )
    snapshot = _safe(snapshot)
    snapshot["notification"] = {"requested": False, "sent": False}
    return snapshot


def render_qq_report(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Render only whitelisted business fields; never interpolate errors or config."""
    day = snapshot.get("trading_day") or {}
    formal = snapshot.get("formal_selection") or {}
    reference = snapshot.get("wencai_reference") or {}
    holdings = snapshot.get("holdings") or {}
    plans = snapshot.get("trade_plans") or {}
    top5 = formal.get("formal_top5") or []
    risk = plans.get("portfolio_risk") or {}
    lines = [
        f"日期：{day.get('date') or '未知'}；交易日证据："
        f"{'已确认' if day.get('confirmed') else '未知'}",
        f"正式选股：TOP15 {len(formal.get('formal_top15') or [])} 只，TOP5 {len(top5)} 只；"
        f"状态 {formal.get('status') or 'missing'}",
    ]
    if top5:
        labels = [f"{row.get('name') or row.get('symbol')}({row.get('symbol')})" for row in top5]
        lines.append("TOP5：" + "、".join(labels))
    lines.extend([
        f"问财参考：{reference.get('ready_groups') or 0}/5 组可用（仅参考，不影响正式候选）",
        f"真实持仓：{holdings.get('count') if holdings.get('count') is not None else '未知'} 只；"
        f"状态 {holdings.get('status') or 'missing'}",
        f"持仓风控：{risk.get('summary') or '暂无有效结果'}；"
        f"状态 {plans.get('status') or 'missing'}",
        f"快照质量：{snapshot.get('status') or 'degraded'}；仅供研究，不自动下单。",
    ])
    return "ShadowFoliant 计划报告", "\n".join(lines)


def send_qq(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not os.getenv("QQ_WEBHOOK_URL", "").strip():
        return {"requested": True, "sent": False, "error_code": "qq_webhook_missing",
                "repair_hint": "Set QQ_WEBHOOK_URL outside the repository."}
    title, content = render_qq_report(snapshot)
    try:
        from notify import notification_router

        with contextlib.redirect_stdout(io.StringIO()):
            result = notification_router.send(
                "report", title, content, only_channels=["qq"], fallback=None,
            )
        sent = bool((result.get("qq") or (False, ""))[0])
    except Exception:
        sent = False
    return ({"requested": True, "sent": True, "channel": "qq"} if sent else
            {"requested": True, "sent": False, "channel": "qq",
             "error_code": "qq_delivery_failed"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one bounded Foliant scheduled snapshot")
    parser.add_argument("--send-qq", action="store_true", help="explicitly send a compact QQ report")
    args = parser.parse_args(argv)
    snapshot = fetch_snapshot()
    if args.send_qq:
        snapshot["notification"] = send_qq(snapshot)
    print(json.dumps(_safe(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if snapshot.get("status") in {"complete", "degraded"} else 2


if __name__ == "__main__":
    sys.exit(main())
