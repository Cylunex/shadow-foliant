"""Fetch one bounded Foliant snapshot for cron/Codex heartbeats.

Configuration is repository-external. The command never connects to PostgreSQL and
defaults to no notification.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # Keep the machine-readable CLI independent from cwd without loading .env or
    # enabling the application's timestamped stdout wrapper.
    sys.path.insert(0, str(PROJECT_ROOT))

ENDPOINT = "/api/machine/v1/agent/scheduled-snapshot"
EXTERNAL_ENDPOINT = "/api/machine/v1/agent/external-independent-research"
NOTIFICATION_ENDPOINT = ENDPOINT + "/notification-"
AUDIT_ENDPOINT = ENDPOINT + "/notification-audit"
MAX_EXTERNAL_BUNDLE_BYTES = 262144
EXTERNAL_REJECTION_HINTS = {
    "external_evidence_dedupe_conflict": (
        "Evidence dedupe_key is immutable. Use a new versioned key for a correction; review the earlier evidence."
    ),
    "external_idempotency_key_conflict": (
        "Use a new submission idempotency_key for changed external research."
    ),
    "historical_ranking_backfill_forbidden": (
        "The decision is outside the contemporaneous window; do not backfill ranking or a past QQ slot."
    ),
}
NOTIFICATION_REJECTION_HINTS = {
    "scheduled_notification_slot_outside_window": "Use only the current planned slot; never backfill QQ.",
    "scheduled_notification_actor_mismatch": "Check the protected writer identity; do not retry blindly.",
    "scheduled_notification_slot_invalid": "Use one of the four planned Shanghai slots.",
}
SCHEDULED_NOTIFICATION_TIMES = ("10:15", "11:25", "14:35", "20:45")
QQ_SUMMARY_VERSION = "scheduled-qq-v2"
DELIVERY_RECEIPT_VERSION = "scheduled-delivery-receipt-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
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


def _load_external_token() -> str:
    token_file = os.getenv("FOLIANT_EXTERNAL_RESEARCH_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text("utf-8").strip()
        except (OSError, UnicodeError):
            return ""
    return os.getenv("FOLIANT_EXTERNAL_RESEARCH_TOKEN", "").strip()


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
    if (isinstance(payload, dict) and payload.get("data") is None
            and "inline result was truncated" in (payload.get("warnings") or [])):
        failure = _failure(
            "agent_snapshot_truncated",
            "The protected Agent snapshot exceeded its inline budget; do not notify.",
            status="degraded",
        )
        transport = ((payload.get("continuation") or {}).get("transport") or {})
        if isinstance(transport, dict):
            sizes = {
                str(key): int(value)
                for key, value in (transport.get("section_bytes") or {}).items()
                if isinstance(key, str) and isinstance(value, int) and value >= 0
            }
            failure["error"]["transport"] = {
                key: int(transport[key])
                for key in ("max_bytes", "uncompressed_bytes", "gzip_bytes")
                if isinstance(transport.get(key), int) and transport[key] >= 0
            }
            if sizes:
                failure["error"]["transport"]["section_bytes"] = sizes
        return failure
    snapshot = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != "scheduled-agent-snapshot-v1":
        return _failure(
            "agent_contract_mismatch", "Deploy a Foliant version exposing scheduled-agent-snapshot-v1.",
            status="degraded",
        )
    snapshot = _safe(snapshot)
    snapshot["notification"] = {"requested": False, "sent": False}
    return snapshot


def _load_external_bundle(path_value: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = Path(path_value)
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("external_bundle_not_regular_file")
        if path.stat().st_size > MAX_EXTERNAL_BUNDLE_BYTES:
            raise ValueError("external_bundle_too_large")
        raw = json.loads(path.read_text("utf-8"))
        from webui.external_research_routes import ExternalIndependentBundleReq

        validated = ExternalIndependentBundleReq.model_validate(raw)
        bundle = validated.model_dump()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return None, _failure(
            "external_bundle_invalid",
            "Provide one strict codex-external-independent-v1 JSON file under 256 KiB.",
        )
    return bundle, None


def _external_client(endpoint: str):
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
    token = _load_external_token()
    if not token:
        return None, _failure(
            "external_research_token_missing",
            "Set FOLIANT_EXTERNAL_RESEARCH_TOKEN_FILE outside the repository.",
        )
    try:
        timeout = min(60.0, max(1.0, float(os.getenv("FOLIANT_AGENT_TIMEOUT_SECONDS", "30"))))
    except ValueError:
        timeout = 30.0
    return (base_url + endpoint, token, timeout), None


def _external_post(endpoint: str, body: dict[str, Any], failure_code: str):
    client, failure = _external_client(endpoint)
    if failure:
        return None, failure
    url, token, timeout = client
    try:
        response = requests.post(
            url, json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException:
        return None, _failure(failure_code, "Verify the protected Foliant service connectivity.", status="degraded")
    if response.status_code in {401, 403}:
        return None, _failure(
            "external_research_authorization_failed",
            "Provision a separate writer with stock.research and foliant.selection.preview.",
        )
    if response.status_code != 200:
        if response.status_code in {400, 409, 422}:
            try:
                error = (response.json() or {}).get("error") or {}
                code = error.get("code")
            except (ValueError, AttributeError, TypeError):
                code = None
            hints = EXTERNAL_REJECTION_HINTS | NOTIFICATION_REJECTION_HINTS
            if isinstance(code, str) and code in hints:
                return None, _failure(code, hints[code], status="degraded")
        return None, _failure(failure_code, "Inspect protected Foliant logs by request time.", status="degraded")
    try:
        payload = response.json()
    except ValueError:
        return None, _failure(failure_code, "The protected Foliant response was invalid.", status="degraded")
    return _safe(payload), None


def submit_external_bundle(bundle: dict[str, Any]):
    return _external_post(EXTERNAL_ENDPOINT, bundle, "external_research_submit_failed")


def notification_ledger(action: str, body: dict[str, Any]):
    if action not in {"claim", "start", "finish"}:
        raise ValueError("notification_action_invalid")
    return _external_post(NOTIFICATION_ENDPOINT + action, body,
                          "notification_ledger_unavailable")


def fetch_notification_audit() -> dict[str, Any]:
    client, failure = _configured_client()
    if failure:
        return failure
    url, token, timeout = client
    try:
        response = requests.get(
            url.replace(ENDPOINT, AUDIT_ENDPOINT),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            raise ValueError("audit_response_failed")
        payload = response.json()
        rows = (payload.get("data") or {}).get("rows")
        if not isinstance(rows, list):
            raise ValueError("audit_response_invalid")
        return {"schema_version": "scheduled-notification-audit-v1", "rows": rows[:56]}
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return _failure("notification_audit_unavailable",
                        "Inspect the protected audit endpoint and authorization.",
                        status="degraded")


def scheduled_notification_slot(
    snapshot: dict[str, Any], *, scheduled_time: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Return the current planned report slot in Asia/Shanghai.

    Explicit scheduled times are intended for cron definitions. Automatic mode
    selects the latest due slot, so a delayed retry remains in the same bounded
    slot until the next planned report time.
    """
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    report_date = str((snapshot.get("trading_day") or {}).get("date") or "")
    trading_day = snapshot.get("trading_day") or {}
    if trading_day.get("confirmed") is not True or trading_day.get("is_trading_day") is not True:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        return None
    target = str(scheduled_time or "").strip()
    if target:
        if target not in SCHEDULED_NOTIFICATION_TIMES:
            return None
    else:
        if report_date != current.date().isoformat():
            return None
        minute = current.hour * 60 + current.minute
        due = [
            value for value in SCHEDULED_NOTIFICATION_TIMES
            if int(value[:2]) * 60 + int(value[3:]) <= minute
        ]
        if not due:
            return None
        target = due[-1]
    return f"{report_date}T{target}+08:00"


def _merge_external_submission(snapshot: dict[str, Any], submission: dict[str, Any]):
    submitted = (submission.get("data") or {}).get("overlay") or {}
    merged = snapshot.get("external_independent_research") or {}
    required = (
        merged.get("status") == "complete"
        and merged.get("overlay_id") == submitted.get("overlay_id")
        and merged.get("idempotency_key") == submitted.get("idempotency_key")
        and merged.get("selection_run_id") == submitted.get("selection_run_id")
        and bool(merged.get("decision_as_of"))
        and bool(merged.get("ranking_locked_at"))
    )
    if not required:
        return _failure(
            "external_research_snapshot_mismatch",
            "Do not notify; refresh only after the submitted overlay is visible in the same snapshot.",
            status="degraded",
        )
    return None


def _degrade_external_submission(
    snapshot: dict[str, Any], failure: dict[str, Any],
    *, selection_run_id: str | None = None,
) -> dict[str, Any]:
    """Preserve the independent report while refusing to present rejected research."""
    error = failure.get("error") or {}
    code = str(error.get("code") or "external_research_submit_failed")
    detail = {
        "status": "rejected" if code in EXTERNAL_REJECTION_HINTS else "unavailable",
        "error_code": code,
        "repair_hint": str(error.get("repair_hint") or "Inspect the external research submission."),
        "selection_run_id": selection_run_id,
    }
    snapshot["external_submission"] = detail
    if snapshot.get("schema_version") != "scheduled-agent-snapshot-v1" or snapshot.get("error"):
        return snapshot
    snapshot["status"] = "degraded"
    snapshot["external_independent_research"] = {
        "status": "degraded", "channel": "codex-external-independent-v1",
        "submission_status": detail["status"], "error_code": code,
        "top15": [], "top5": [], "evidence": [], "news_watchlist": [],
        "tuning_proposals": [], "formal_membership_unchanged": True,
        "human_review_required": True, "auto_apply": False, "auto_execution": False,
    }
    quality = snapshot.get("quality")
    if isinstance(quality, dict):
        quality["status"] = "degraded"
        sections = quality.get("sections")
        if isinstance(sections, dict):
            sections["external_independent_research"] = "degraded"
        optional = quality.get("optional_degradations")
        if isinstance(optional, list) and "external_independent_research" not in optional:
            optional.append("external_independent_research")
    comparison = snapshot.get("source_comparison")
    if isinstance(comparison, dict):
        comparison["external_top5"] = None
        availability = comparison.get("availability")
        if isinstance(availability, dict):
            availability["external_independent"] = False
    as_of = snapshot.get("as_of")
    if isinstance(as_of, dict):
        as_of["external_independent_research"] = None
    return snapshot


def render_qq_report(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Render only whitelisted business fields; never interpolate errors or config."""
    day = snapshot.get("trading_day") or {}
    formal = snapshot.get("formal_selection") or {}
    independent = snapshot.get("independent_selection") or {}
    external = snapshot.get("external_independent_research") or {}
    reference = snapshot.get("wencai_reference") or {}
    openapi_shadow = snapshot.get("iwencai_openapi_shadow") or {}
    miaoxiang = snapshot.get("miaoxiang_reference") or {}
    cockpit = snapshot.get("cockpit") or {}
    policy = cockpit.get("portfolio_policy") or {}
    market_signal = policy.get("market_add_signal") or {}
    holdings = snapshot.get("holdings") or {}
    industry = snapshot.get("portfolio_industry") or {}
    plans = snapshot.get("trade_plans") or {}
    post_close = snapshot.get("post_close_review") or {}
    proposals = snapshot.get("strategy_adjustment_proposals") or {}
    top5 = formal.get("formal_top5") or []
    risk = plans.get("portfolio_risk") or {}
    stock_budget = (plans.get("cash_policy") or {}).get("stock_budget") or {}
    lines = [
        f"日期：{day.get('date') or '未知'}；交易日证据："
        f"{'已确认' if day.get('confirmed') else '未知'}",
        f"正式选股（{formal.get('display_name') or '正式本地PIT融合榜'}）："
        f"TOP15 {len(formal.get('formal_top15') or [])} 只，TOP5 {len(top5)} 只；"
        f"状态 {formal.get('status') or 'missing'}",
    ]
    if top5:
        labels = [f"{row.get('name') or row.get('symbol')}({row.get('symbol')})" for row in top5]
        lines.append("TOP5：" + "、".join(labels))
    independent_top5 = independent.get("top5") or []
    if independent.get("status") == "complete":
        labels = [
            f"{row.get('name') or row.get('symbol')}({row.get('symbol')})"
            for row in independent_top5
        ]
        lines.append("独立TOP5：" + ("、".join(labels) or "无候选"))
    else:
        lines.append("独立TOP5：不可用（必要输入不完整）")
    if independent.get("market_as_of"):
        lines.append(
            f"独立量化时点：{independent.get('selection_session_date') or '未知'} 选择，"
            f"PIT 行情输入截至 {independent.get('market_as_of')}；"
            "不等同于当日盘后新研究。"
        )
    compatibility = independent.get("version_compatibility") or {}
    if compatibility:
        lines.append(
            f"独立量化版本：{independent.get('strategy_version') or '未知'}；"
            f"兼容状态 {compatibility.get('status') or 'unknown'}；"
            f"权重契约 {'保持' if compatibility.get('weight_contract_preserved') else '不匹配'}；"
            "v1 外部叠加仅保留为历史，不回填。"
        )
    if external.get("status") == "complete":
        labels = [
            f"{row.get('name') or row.get('symbol')}({row.get('symbol')})"
            for row in external.get("top5") or []
        ]
        lines.append(
            f"外部独立：{external.get('market_regime') or 'unknown'}；TOP5 "
            f"{'、'.join(labels) or '无候选'}；证据 {len(external.get('evidence') or [])} 条；"
            f"decision {external.get('decision_as_of') or '不可用'}；"
            f"locked {external.get('ranking_locked_at') or '不可用'}"
        )
    elif external.get("submission_status") in {"rejected", "unavailable"}:
        lines.append(
            "外部独立研究：本次提交未通过，今日外部排序与事件调整未采用；"
            "以下正式选股、持仓和风控取自可用快照。"
        )
    elif external.get("status") in {"stale", "missing", "degraded"}:
        reasons = ",".join(external.get("stale_reason_codes") or []) or "not_current"
        lines.append(
            "外部独立研究：今日不可用或已过期，旧排名和个股事件加分不参与判断；"
            f"仅对比正式选股与独立量化底座（原因 {reasons}）。"
        )
    lines.extend([
        (f"问财 OpenAPI 试运行参考：{reference.get('trial_data_groups') or 0}/5 组有数据；"
         + (f"{reference.get('semantic_verified_groups') or 0}/5 组语义核验通过；"
            if reference.get('semantic_equivalence_verified') else
            "语义尚未全部核验；")
         + "非正式输入，不影响正式候选。"
         if reference.get('source_mode') == 'openapi_trial' else
         f"问财参考：{reference.get('ready_groups') or 0}/5 组可用（仅参考，不影响正式候选）"),
        (
            "问财 OpenAPI 影子：独立 Key 未配置，未试跑；不替换旧问财。"
            if openapi_shadow.get('status') == 'credential_missing' else
            f"问财 OpenAPI 影子：{openapi_shadow.get('data_groups') or 0}/5 组有数据，"
            f"{openapi_shadow.get('semantic_verified_groups') or 0}/5 组通过语义与排名校验；"
            f"当日调用 {(openapi_shadow.get('usage') or {}).get('calls_today') if (openapi_shadow.get('usage') or {}).get('calls_today') is not None else '未知'}/"
            f"{(openapi_shadow.get('usage') or {}).get('hard_daily_limit') or 70}；"
            f"状态 {openapi_shadow.get('status') or 'missing'}；不参与正式排名。"
        ),
        (
            ('问财切换：OpenAPI 试运行参考已授权并启用；五组语义与排名核验通过，'
             '仍仅作参考，正式选股不变，无需再次审批。'
             if reference.get('semantic_equivalence_verified') else
             '问财切换：OpenAPI 试运行参考已授权并启用；'
             '原五组语义等价性仍未全部证实，正式选股不变，无需再次审批。')
            if reference.get('source_mode') == 'openapi_trial' else
            ('问财切换：entitlement_or_semantic_blocked；原五组等价性未证实，' +
             '旧源继续作为参考。后续可升级权益、维持降级，或明确重定义策略；'
             '不会因连续返回名单而认定等价。')
            if openapi_shadow.get('replacement_status') == 'entitlement_or_semantic_blocked'
            else '问财切换：语义未核验时不认定为等价。'
        ),
        (
            f"妙想参考：{miaoxiang.get('ready_groups') or 0}/5 组；"
            f"诊断买入{(miaoxiang.get('diagnosis') or {}).get('buy') or 0}/"
            f"观望{(miaoxiang.get('diagnosis') or {}).get('watch') or 0}/"
            f"规避{(miaoxiang.get('diagnosis') or {}).get('avoid') or 0}/"
            f"失败{(miaoxiang.get('diagnosis') or {}).get('failed') or 0}；"
            f"{'无分歧，按规则不单独推送' if miaoxiang.get('notification_reason') == 'no_disagreement_no_push' else '仅供对照，不改变正式选股'}；"
            f"状态 {miaoxiang.get('status') or 'missing'}。"
        ),
        f"真实持仓：{holdings.get('count') if holdings.get('count') is not None else '未知'} 只；"
        f"状态 {holdings.get('status') or 'missing'}",
        (
            f"申万行业：股票 {industry.get('stock_count', '未知')} 只、"
            f"基金排除 {industry.get('excluded_fund_count', '未知')} 只；"
            f"代码覆盖 {float(industry.get('coverage') or 0):.1%}；"
            + (
                f"同行比较可用，弱势观察 {industry.get('pruning_observation_count') or 0} 只"
                "（需连续确认，非交易指令）"
                if industry.get("peer_comparison_status") == "complete" else
                f"同行比较部分可用，弱势观察 {industry.get('pruning_observation_count') or 0} 只"
                "（数据缺口详见快照）"
                if industry.get("peer_comparison_status") == "partial" else
                "同行比较暂停（历史数据或质量门槛不足）"
            )
        ),
        (
            f"股票预算：¥{float(stock_budget.get('total_budget_cny') or 0):,.0f}；"
            f"股票 {stock_budget.get('stock_holding_count', '未知')} 只/市值 "
            f"¥{float(stock_budget.get('stock_market_value_cny') or 0):,.0f}；"
            f"基金排除 {stock_budget.get('excluded_fund_holding_count', '未知')} 只；"
            f"可用 ¥{float(stock_budget.get('available_cash_cny') or 0):,.0f}；"
            f"as-of {stock_budget.get('as_of') or '不可用'}"
            if stock_budget.get("status") == "complete" else
            "股票预算：分类或行情不完整，买入侧失败关闭；卖出复核与完整持仓报告继续。"
        ),
        f"持仓风控：{risk.get('summary') or '暂无有效结果'}；"
        f"状态 {plans.get('status') or 'missing'}",
        f"快照质量：{snapshot.get('status') or 'degraded'}；"
        f"阶段 {snapshot.get('phase') or 'unknown'}；仅供研究，不自动下单。",
    ])
    if policy.get("fail_closed"):
        failure_code = str(market_signal.get("source_failure_code") or "")
        if not re.fullmatch(r"[a-z0-9_]{1,80}", failure_code):
            failure_code = "数据不完整"
        lines.append(f"组合买入门：失败关闭（{failure_code}），不依据缺失数据加仓。")
    groups = (industry.get("industry_groups") or [])[:3]
    if groups:
        labels = [
            f"{row.get('industry_l1_name') or row.get('industry_l1_code')}"
            f"{row.get('holding_count')}只"
            for row in groups
        ]
        lines.append("持仓一级行业（前三）：" + "、".join(labels))
    if post_close.get("due"):
        lines.append(f"盘后结论：{post_close.get('conclusion') or '盘后闭环结果不可用。'}")
        holding_review = snapshot.get("holdings_review") or {}
        next_plan = snapshot.get("next_session_plan") or {}
        strategy_evidence = post_close.get("selection_strategy_evidence") or {}
        missing_plans = sorted(set(
            (holding_review.get("unusable_trade_plan_symbols") or [])
            + (next_plan.get("unusable_trade_plan_symbols") or [])
        ))[:20]
        lines.append(
            f"盘后持仓复盘：{holding_review.get('reviewed_count') or 0}/"
            f"{holding_review.get('count') or 0} 只已核；次日计划 "
            f"{next_plan.get('ready_count') or 0}/{next_plan.get('count') or 0} 条可用。"
            + (f"权威计划缺失、不可用或未按当日收盘重建：{','.join(missing_plans)}，"
               "对应标的旧阈值仅作历史参考。"
               if missing_plans else "")
        )
        if strategy_evidence.get("status") != "complete":
            lines.append(
                "策略统计：完整性降级；缺窗口 "
                f"{','.join(map(str, strategy_evidence.get('missing_horizons_days') or [])) or '无'}；"
                "缺来源 "
                f"{','.join(strategy_evidence.get('missing_source_outcomes') or []) or '无'}；"
                "作业成功不代表统计完整。"
            )
        lines.append(
            f"策略调整：{proposals.get('proposal_count') or 0} 项待复核；"
            "仅生成建议，不自动应用。"
        )
    return "ShadowFoliant 计划报告", "\n".join(lines)


def render_qq_summary(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Eight priority-ordered lines. The protected snapshot remains the full report."""
    from notify.plain_language import plain_text

    def short(value: Any, limit: int = 100) -> str:
        return plain_text(str(value or "未知").replace("\n", " "), limit)

    day = snapshot.get("trading_day") or {}
    holdings = snapshot.get("holdings") or {}
    plans = snapshot.get("trade_plans") or {}
    review = snapshot.get("holdings_review") or {}
    next_plan = snapshot.get("next_session_plan") or {}
    post_close = snapshot.get("post_close_review") or {}
    cash = plans.get("cash_policy") or {}
    budget = cash.get("stock_budget") or {}
    formal = snapshot.get("formal_selection") or {}
    independent = snapshot.get("independent_selection") or {}
    external = snapshot.get("external_independent_research") or {}
    comparison = snapshot.get("source_comparison") or {}
    authority = plans.get("holding_actions_authority") or {}
    if post_close.get("due"):
        actions = review.get("rows") or []
        authority_text = "当日收盘复盘" if review.get("status") in {"complete", "degraded"} else "盘后动作不可用"
    else:
        actions = plans.get("holding_actions") or [] if authority.get("status") == "current" else []
        authority_text = "同批行情持仓动作" if authority.get("status") == "current" else "当期动作不可用，旧信号不作依据"
    sells = [row for row in actions if row.get("action") in {"sell", "reduce"}]
    sell_labels = [short(row.get("name") or row.get("symbol") or row.get("code"), 14)
                   for row in sells[:3]]
    remainder = f"等{len(sells)}只" if len(sells) > 3 else ""
    action_text = ("、".join(sell_labels) + remainder) if sells else "无卖出/减仓信号"
    action_line = (f"权威动作（{authority_text}）：卖出/减仓{len(sells)}只，{action_text}；"
                   f"持仓{holdings.get('count', '未知')}只。")
    if post_close.get("due"):
        invalid = sorted(set((review.get("unusable_trade_plan_symbols") or [])
                             + (next_plan.get("unusable_trade_plan_symbols") or [])))
        condition = (f"失效：{len(invalid)}只权威计划不可用，旧价位无效（例：{','.join(invalid[:3])}）。"
                     if invalid else "失效条件：次日行情、现金及可卖量须重新确认。")
    else:
        condition = ("失效条件：若行情批次/计划不匹配，停止使用上述动作。"
                     if authority.get("status") == "current" else
                     "失效条件：行情或计划未绑定当期，停止使用旧动作。")
    condition = f"风控{short((plans.get('portfolio_risk') or {}).get('summary'), 25)}；{condition}"
    if budget.get("status") == "complete":
        cash_text = (f"股票预算可用¥{float(budget.get('available_cash_cny') or 0):,.0f}；"
                     f"现金口径{short(budget.get('as_of') or '未知', 25)}；"
                     "仅预览，不允许自动买入。")
    else:
        cash_text = "股票预算/现金质量不完整；买入侧关闭，卖出复核继续。"
    if cash.get("buy_side", {}).get("status") == "blocked" or cash.get("new_or_add_positions_allowed") is False:
        cash_text += "买入不放行。"
    external_status = external.get("status")
    if external_status == "complete":
        external_text = f"外部独立当期锁定{len(external.get('top5') or [])}只"
    else:
        external_text = "外部独立当期无效，旧排名不采用"
    picks = (f"三方：正式{formal.get('status') or 'missing'} TOP5={len(formal.get('formal_top5') or [])}；"
             f"独立{independent.get('status') or 'missing'} TOP5={len(independent.get('top5') or [])}；"
             f"{external_text}。")
    pairs = comparison.get("pairwise") or {}
    fi = pairs.get("formal_independent") or {}
    overlap = len(fi.get("intersection") or []) if fi else None
    formal_symbols = {str(row.get("symbol")) for row in formal.get("formal_top5") or []
                      if row.get("symbol")}
    external_symbols = {str(row.get("symbol")) for row in external.get("top5") or []
                        if row.get("symbol")}
    external_difference = (f"正式/外部交集{len(formal_symbols & external_symbols)}只；"
                           if external_status == "complete" and formal_symbols else "")
    difference = (f"正式/独立交集{overlap}只；{external_difference}"
                  f"差异正式{len(fi.get('formal_only') or [])}只、"
                  f"独立{len(fi.get('independent_only') or [])}只。"
                  if overlap is not None else "三方差异：当期对比不可用。")
    if post_close.get("due"):
        plan_count = int(next_plan.get("count") or 0)
        ready_count = int(next_plan.get("ready_count") or 0)
        blocked_count = int(next_plan.get("blocked_count") or max(0, plan_count - ready_count))
        close_line = (f"盘后：{short(post_close.get('conclusion'), 65)}；"
                      f"持仓复盘{review.get('reviewed_count') or 0}/{review.get('count') or 0}；"
                      f"次日计划{ready_count}/{plan_count}可用，缺口{blocked_count}。")
    else:
        close_line = f"盘后/次日计划：{short(snapshot.get('phase'), 25)}阶段未到期；下一时点复核。"
    lines = [
        f"{day.get('date') or '未知日期'} {short(snapshot.get('phase'), 25)}；质量{short(snapshot.get('status'), 15)}。",
        action_line, condition, cash_text, picks, difference, close_line,
        "QQ仅为有界摘要，未覆盖全部持仓；完整持仓与失效证据请走受保护快照。",
    ]
    # Every priority category has its own line; trim within each line before
    # the generic router's eight-line/900-character transport limit applies.
    lines = [short(line, 108) for line in lines]
    return "ShadowFoliant 计划摘要", "\n".join(lines)


def qq_preflight(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    required = (
        "trading_day", "formal_selection", "holdings", "trade_plans", "quotes",
        "post_close_review", "holdings_review", "next_session_plan", "as_of", "quality",
    )
    if (not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != "scheduled-agent-snapshot-v1"
            or snapshot.get("status") not in {"complete", "degraded"}
            or snapshot.get("error")
            or any(not isinstance(snapshot.get(key), dict) for key in required)
            or not (snapshot.get("trading_day") or {}).get("date")
            or not (snapshot.get("as_of") or {}).get("captured_at")
            or not (snapshot.get("quality") or {}).get("status")):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "snapshot_contract_incomplete"}
    day = snapshot["trading_day"]
    if day.get("confirmed") is not True or day.get("is_trading_day") is not True:
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "trading_day_not_confirmed_open"}
    if (snapshot["post_close_review"].get("due")
            and (snapshot["post_close_review"].get("status") not in {"complete", "degraded"}
                 or snapshot["holdings_review"].get("status") not in {"complete", "degraded"}
                 or snapshot["next_session_plan"].get("status") not in {"complete", "degraded"}
                 or not snapshot["post_close_review"].get("conclusion"))):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "post_close_review_incomplete"}
    if not os.getenv("QQ_WEBHOOK_URL", "").strip():
        return {"requested": True, "sent": False, "error_code": "qq_webhook_missing",
                "repair_hint": "Set QQ_WEBHOOK_URL outside the repository."}
    return None


def qq_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    from notify.plain_language import compact_notification
    title, content = render_qq_summary(snapshot)
    delivered = compact_notification("report", content)
    if delivered != content or len(delivered.splitlines()) != 8 or len(delivered) > 900:
        raise ValueError("qq_summary_exceeds_transport_budget")
    return {
        "title": title, "content": content,
        "payload_hash": hashlib.sha256((title + "\n" + delivered).encode("utf-8")).hexdigest(),
        "original_lines": len(render_qq_report(snapshot)[1].splitlines()),
        "delivered_lines": len(delivered.splitlines()),
        "category": "report", "version": QQ_SUMMARY_VERSION,
    }


def send_qq(snapshot: dict[str, Any], *, payload: dict[str, Any] | None = None,
            notification_slot: str | None = None) -> dict[str, Any]:
    failed = qq_preflight(snapshot)
    if failed:
        return failed
    try:
        payload = payload or qq_payload(snapshot)
    except Exception:
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_summary_invalid"}
    try:
        from notify import notification_router
    except (ImportError, ModuleNotFoundError):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_router_unavailable"}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = notification_router.send(
                "report", payload["title"], payload["content"],
                only_channels=["qq"], fallback=None,
                source="scripts.foliant_scheduled_snapshot",
                source_run_id=str(snapshot.get("run_id") or "") or None,
                idempotency_key="scheduled-qq:" + hashlib.sha256(
                    (notification_slot or payload["payload_hash"]).encode("utf-8")
                ).hexdigest(),
                business_as_of=str((snapshot.get("as_of") or {}).get("captured_at") or "") or None,
                original_body=render_qq_report(snapshot)[1],
                externally_deduplicated=True,
            )
    except Exception:
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_router_error"}
    if not isinstance(result, dict) or not isinstance(result.get("qq"), tuple):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_router_result_invalid"}
    try:
        sent = bool(result["qq"][0])
    except Exception:
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_router_result_invalid"}
    detail = str(result["qq"][1] or "")
    match = re.fullmatch(r"HTTP (\d{3})", detail)
    http_status = int(match.group(1)) if match else None
    archive_status = getattr(result, "archive_status", "unobserved")
    archive_message_id = getattr(result, "message_id", None)
    return ({"requested": True, "sent": True, "channel": "qq",
             "delivery_status": "delivered", "http_status": http_status,
             "message_archive_status": archive_status,
             "message_archive_id": archive_message_id} if sent else
            {"requested": True, "sent": False, "channel": "qq",
             "delivery_status": "failed" if http_status else "unknown",
             "http_status": http_status,
             "message_archive_status": archive_status,
             "message_archive_id": archive_message_id,
             "error_code": "qq_http_rejected" if http_status else "qq_delivery_unknown"})


def delivery_receipt(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Bounded machine result for one requested send; never include report bodies."""
    notification = snapshot.get("notification")
    notification = notification if isinstance(notification, dict) else {}
    as_of = snapshot.get("as_of")
    as_of = as_of if isinstance(as_of, dict) else {}
    submission = snapshot.get("external_submission")
    submission = submission if isinstance(submission, dict) else {}

    def short(value: Any, length: int = 96) -> str | None:
        return str(value)[:length] if value is not None else None

    def bounded_int(value: Any) -> int | None:
        return value if type(value) is int and 0 <= value <= 1000000 else None

    fields = (
        "channel", "notification_slot", "delivery_status", "suppression_reason",
        "delivered_at", "error_code", "payload_hash", "summary_version",
        "message_archive_status", "message_archive_id",
    )
    result = {key: short(notification.get(key)) for key in fields}
    result.update({
        "requested": bool(notification.get("requested")),
        "sent": bool(notification.get("sent")),
        "delivery_recorded": (bool(notification["delivery_recorded"])
                              if "delivery_recorded" in notification else None),
        "prior_sent": bool(notification.get("prior_sent")),
        "suppressed": bool(notification.get("suppressed")),
        "http_status": bounded_int(notification.get("http_status")),
        "original_lines": bounded_int(notification.get("original_lines")),
        "delivered_lines": bounded_int(notification.get("delivered_lines")),
    })
    return {
        "schema_version": DELIVERY_RECEIPT_VERSION,
        "snapshot_schema_version": short(snapshot.get("schema_version"), 80),
        "status": short(snapshot.get("status"), 80),
        "observed_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "snapshot_as_of": short(as_of.get("captured_at"), 80),
        "external_submission_status": short(submission.get("status"), 80),
        "external_submission_error_code": short(submission.get("error_code")),
        "notification": result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one bounded Foliant scheduled snapshot")
    parser.add_argument("--send-qq", action="store_true", help="explicitly send a compact QQ report")
    parser.add_argument("--delivery-receipt", action="store_true",
                        help="with --send-qq, print only a bounded machine-readable delivery receipt")
    parser.add_argument("--audit-notifications", action="store_true",
                        help="read a bounded, redacted 14-day slot audit; never send")
    parser.add_argument(
        "--notification-slot", choices=SCHEDULED_NOTIFICATION_TIMES,
        help="planned Asia/Shanghai report time; defaults to the latest due slot",
    )
    parser.add_argument(
        "--external-bundle",
        help="strict codex-external-independent-v1 JSON submitted before snapshot retrieval",
    )
    args = parser.parse_args(argv)
    if args.delivery_receipt and not args.send_qq:
        parser.error("--delivery-receipt requires --send-qq")
    if args.audit_notifications:
        if args.send_qq or args.external_bundle or args.delivery_receipt:
            parser.error("--audit-notifications cannot submit research or send QQ")
        audit = fetch_notification_audit()
        print(json.dumps(_safe(audit), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")))
        return 0 if audit.get("schema_version") == "scheduled-notification-audit-v1" else 2
    submission = None
    external_failure = None
    bundle = None
    if args.external_bundle:
        bundle, external_failure = _load_external_bundle(args.external_bundle)
        if bundle is not None:
            submission, external_failure = submit_external_bundle(bundle)
    snapshot = fetch_snapshot()
    if external_failure:
        snapshot = _degrade_external_submission(
            snapshot, external_failure,
            selection_run_id=(bundle or {}).get("selection_run_id"),
        )
    if submission:
        failure = _merge_external_submission(snapshot, submission)
        if failure:
            external_failure = failure
            snapshot = _degrade_external_submission(
                snapshot, failure, selection_run_id=(bundle or {}).get("selection_run_id"),
            )
    if args.send_qq:
        if not submission and not external_failure:
            snapshot = _degrade_external_submission(
                snapshot,
                _failure("external_current_slot_unsubmitted",
                         "Current-slot external ranking was not submitted; use formal and portfolio evidence only.",
                         status="degraded"),
            )
        notification_slot = scheduled_notification_slot(
            snapshot, scheduled_time=args.notification_slot)
        failed = qq_preflight(snapshot)
        if not notification_slot:
            snapshot["notification"] = {"requested": True, "sent": False,
                "error_code": "scheduled_notification_slot_unavailable"}
        elif failed:
            snapshot["notification"] = failed | {"notification_slot": notification_slot}
        else:
            try:
                payload = qq_payload(snapshot)
            except Exception:
                snapshot["notification"] = {"requested": True, "sent": False,
                    "notification_slot": notification_slot,
                    "error_code": "qq_summary_invalid"}
            else:
                claim, claim_failure = notification_ledger("claim", {
                    "notification_slot": notification_slot,
                    **{key: payload[key] for key in (
                        "payload_hash", "original_lines", "delivered_lines",
                        "category", "version")},
                })
                claim_data = (claim or {}).get("data") or {}
                if claim_failure:
                    snapshot["notification"] = {"requested": True, "sent": False,
                        "notification_slot": notification_slot,
                        "error_code": (claim_failure.get("error") or {}).get("code")}
                elif not claim_data.get("should_send") or not claim_data.get("payload_matches"):
                    snapshot["notification"] = {"requested": True, "sent": False,
                        "prior_sent": bool(claim_data.get("prior_sent")),
                        "suppressed": True, "notification_slot": notification_slot,
                        "delivery_status": claim_data.get("delivery_status") or "unknown",
                        "suppression_reason": claim_data.get("suppression_reason"),
                        "delivered_at": claim_data.get("delivered_at")}
                else:
                    start, start_failure = notification_ledger("start", {
                        "notification_slot": notification_slot,
                        "payload_hash": payload["payload_hash"],
                    })
                    if start_failure or not ((start or {}).get("data") or {}).get("started"):
                        snapshot["notification"] = {"requested": True, "sent": False,
                            "notification_slot": notification_slot,
                            "delivery_status": "unknown",
                            "error_code": "notification_start_unconfirmed"}
                    else:
                        notification = send_qq(snapshot, payload=payload,
                                               notification_slot=notification_slot)
                        status = notification.get("delivery_status") or "unknown"
                        finish, finish_failure = notification_ledger("finish", {
                            "notification_slot": notification_slot,
                            "payload_hash": payload["payload_hash"],
                            "status": status,
                            "http_status": notification.get("http_status"),
                            "error_code": notification.get("error_code"),
                        })
                        notification.update({
                            "notification_slot": notification_slot,
                            "payload_hash": payload["payload_hash"],
                            "summary_version": payload["version"],
                            "original_lines": payload["original_lines"],
                            "delivered_lines": payload["delivered_lines"],
                            "delivery_recorded": finish_failure is None and bool(
                                ((finish or {}).get("data") or {}).get("recorded")),
                        })
                        if not notification["delivery_recorded"]:
                            notification["delivery_status"] = "unknown"
                            notification["error_code"] = "notification_finish_unconfirmed"
                        snapshot["notification"] = notification
    output = delivery_receipt(snapshot) if args.delivery_receipt else snapshot
    print(json.dumps(_safe(output), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    notification = snapshot.get("notification") or {}
    notification_failed = bool(
        args.send_qq and not (
            notification.get("prior_sent") or
            (notification.get("sent") and notification.get("delivery_recorded"))
        )
    )
    return 0 if snapshot.get("status") in {"complete", "degraded"} and not notification_failed else 2


if __name__ == "__main__":
    sys.exit(main())
