"""Fetch one bounded Foliant snapshot for cron/Codex heartbeats.

Configuration is repository-external. The command never connects to PostgreSQL and
defaults to no notification.
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
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
EXTERNAL_CLAIM_ENDPOINT = EXTERNAL_ENDPOINT + "/notification-claim"
EXTERNAL_DELIVERY_ENDPOINT = EXTERNAL_ENDPOINT + "/notification-delivery"
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
SCHEDULED_NOTIFICATION_TIMES = ("10:15", "11:25", "14:35", "20:45")
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
        return _failure(
            "agent_snapshot_truncated",
            "The protected Agent snapshot exceeded its inline budget; do not notify.",
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
            if isinstance(code, str) and code in EXTERNAL_REJECTION_HINTS:
                return None, _failure(code, EXTERNAL_REJECTION_HINTS[code], status="degraded")
        return None, _failure(failure_code, "Inspect protected Foliant logs by request time.", status="degraded")
    try:
        payload = response.json()
    except ValueError:
        return None, _failure(failure_code, "The protected Foliant response was invalid.", status="degraded")
    return _safe(payload), None


def submit_external_bundle(bundle: dict[str, Any]):
    return _external_post(EXTERNAL_ENDPOINT, bundle, "external_research_submit_failed")


def claim_external_notification(
    idempotency_key: str, overlay_id: str, notification_slot: str,
):
    return _external_post(EXTERNAL_CLAIM_ENDPOINT, {
        "idempotency_key": idempotency_key,
        "overlay_id": overlay_id,
        "notification_slot": notification_slot,
    }, "external_notification_claim_failed")


def record_external_notification_delivery(
    idempotency_key: str, overlay_id: str, notification_slot: str,
    *, sent: bool, error_code: str | None,
):
    body: dict[str, Any] = {
        "idempotency_key": idempotency_key,
        "overlay_id": overlay_id,
        "notification_slot": notification_slot,
        "sent": bool(sent),
    }
    if error_code:
        body["error_code"] = str(error_code)[:100]
    return _external_post(
        EXTERNAL_DELIVERY_ENDPOINT, body, "external_notification_delivery_record_failed",
    )


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
        f"正式选股：TOP15 {len(formal.get('formal_top15') or [])} 只，TOP5 {len(top5)} 只；"
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
        lines.append(
            "外部独立研究：今日不可用或已过期，旧排名不参与判断；"
            "仅对比正式选股与独立量化底座。"
        )
    lines.extend([
        (f"问财 OpenAPI 试运行参考：{reference.get('trial_data_groups') or 0}/5 组有数据；"
         "语义未核验、非正式输入，不影响正式候选。"
         if reference.get('source_mode') == 'openapi_trial' else
         f"问财参考：{reference.get('ready_groups') or 0}/5 组可用（仅参考，不影响正式候选）"),
        (
            "问财 OpenAPI 影子：独立 Key 未配置，未试跑；不替换旧问财。"
            if openapi_shadow.get('status') == 'credential_missing' else
            f"问财 OpenAPI 影子：{openapi_shadow.get('data_groups') or 0}/5 组有数据，"
            f"{openapi_shadow.get('ready_groups') or 0}/5 组通过完整校验；"
            f"当日调用 {(openapi_shadow.get('usage') or {}).get('calls_today') if (openapi_shadow.get('usage') or {}).get('calls_today') is not None else '未知'}/"
            f"{(openapi_shadow.get('usage') or {}).get('hard_daily_limit') or 70}；"
            f"状态 {openapi_shadow.get('status') or 'missing'}；不参与正式排名。"
        ),
        (
            '问财切换：entitlement_or_semantic_blocked；原五组等价性未证实，' +
            ('OpenAPI 仅作试运行参考，不代表原五组等价；正式选股不变。'
             if reference.get('source_mode') == 'openapi_trial' else
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
            f"同行/剪枝 {'待主题和行情证据' if industry.get('industry_coverage_gate') else '覆盖不足，暂停'}"
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
        missing_plans = sorted(set(
            (holding_review.get("unusable_trade_plan_symbols") or [])
            + (next_plan.get("unusable_trade_plan_symbols") or [])
        ))[:20]
        lines.append(
            f"盘后持仓复盘：{holding_review.get('reviewed_count') or 0}/"
            f"{holding_review.get('count') or 0} 只已核；次日计划 "
            f"{next_plan.get('ready_count') or 0}/{next_plan.get('count') or 0} 条可用。"
            + (f"权威计划缺失或不可用：{','.join(missing_plans)}，对应标的暂不给价。"
               if missing_plans else "")
        )
        lines.append(
            f"策略调整：{proposals.get('proposal_count') or 0} 项待复核；"
            "仅生成建议，不自动应用。"
        )
    return "ShadowFoliant 计划报告", "\n".join(lines)


def send_qq(snapshot: dict[str, Any]) -> dict[str, Any]:
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
    if (snapshot["post_close_review"].get("due")
            and (snapshot["post_close_review"].get("status") != "complete"
                 or snapshot["holdings_review"].get("status") not in {"complete", "degraded"}
                 or snapshot["next_session_plan"].get("status") not in {"complete", "degraded"}
                 or not snapshot["post_close_review"].get("conclusion"))):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "post_close_review_incomplete"}
    if not os.getenv("QQ_WEBHOOK_URL", "").strip():
        return {"requested": True, "sent": False, "error_code": "qq_webhook_missing",
                "repair_hint": "Set QQ_WEBHOOK_URL outside the repository."}
    title, content = render_qq_report(snapshot)
    try:
        from notify import notification_router
    except (ImportError, ModuleNotFoundError):
        return {"requested": True, "sent": False, "channel": "qq",
                "error_code": "qq_router_unavailable"}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = notification_router.send(
                "report", title, content, only_channels=["qq"], fallback=None,
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
    return ({"requested": True, "sent": True, "channel": "qq"} if sent else
            {"requested": True, "sent": False, "channel": "qq",
             "error_code": "qq_delivery_failed"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read one bounded Foliant scheduled snapshot")
    parser.add_argument("--send-qq", action="store_true", help="explicitly send a compact QQ report")
    parser.add_argument(
        "--notification-slot", choices=SCHEDULED_NOTIFICATION_TIMES,
        help="planned Asia/Shanghai report time; defaults to the latest due slot",
    )
    parser.add_argument(
        "--external-bundle",
        help="strict codex-external-independent-v1 JSON submitted before snapshot retrieval",
    )
    args = parser.parse_args(argv)
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
        external = snapshot.get("external_independent_research") or {}
        if external_failure:
            snapshot["notification"] = send_qq(snapshot)
        elif external.get("status") == "complete" and not submission:
            snapshot["notification"] = {
                "requested": True, "sent": False,
                "error_code": "external_submission_claim_required",
            }
        elif submission:
            overlay = (submission.get("data") or {}).get("overlay") or {}
            claim = None
            failure = None
            notification_slot = scheduled_notification_slot(
                snapshot, scheduled_time=args.notification_slot,
            )
            if not notification_slot:
                snapshot["notification"] = {
                    "requested": True, "sent": False,
                    "error_code": "scheduled_notification_slot_unavailable",
                }
            else:
                claim, failure = claim_external_notification(
                    str(overlay.get("idempotency_key") or ""),
                    str(overlay.get("overlay_id") or ""),
                    notification_slot,
                )
            if notification_slot and failure:
                snapshot["notification"] = failure.get("notification") | {
                    "requested": True, "sent": False,
                    "error_code": (failure.get("error") or {}).get("code"),
                }
            elif notification_slot and (claim.get("data") or {}).get("should_send"):
                claim_data = claim.get("data") or {}
                notification = send_qq(snapshot)
                delivery, delivery_failure = record_external_notification_delivery(
                    str(overlay.get("idempotency_key") or ""),
                    str(overlay.get("overlay_id") or ""),
                    notification_slot,
                    sent=bool(notification.get("sent")),
                    error_code=notification.get("error_code"),
                )
                notification.update({
                    "notification_slot": notification_slot,
                    "prior_sent": False,
                    "delivery_status": (
                        (delivery.get("data") or {}).get("delivery_status")
                        if delivery else "record_failed"
                    ),
                    "delivery_recorded": delivery_failure is None,
                })
                if delivery_failure:
                    notification["delivery_record_error_code"] = (
                        (delivery_failure.get("error") or {}).get("code")
                    )
                snapshot["notification"] = notification
            elif notification_slot:
                claim_data = claim.get("data") or {}
                snapshot["notification"] = {
                    "requested": True,
                    "sent": bool(claim_data.get("prior_sent")),
                    "prior_sent": bool(claim_data.get("prior_sent")),
                    "replayed": True,
                    "delivery_status": claim_data.get("delivery_status") or "unknown",
                    "delivered_at": claim_data.get("delivered_at"),
                    "error_code": claim_data.get("delivery_error_code"),
                    "notification_slot": notification_slot,
                }
        else:
            snapshot["notification"] = send_qq(snapshot)
    print(json.dumps(_safe(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    notification = snapshot.get("notification") or {}
    notification_failed = bool(
        args.send_qq and not notification.get("sent")
    )
    return 0 if snapshot.get("status") in {"complete", "degraded"} and not notification_failed else 2


if __name__ == "__main__":
    sys.exit(main())
