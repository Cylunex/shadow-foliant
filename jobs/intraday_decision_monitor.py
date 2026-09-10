"""持仓与正式候选共用的轻量盘中决策快照。

固定节点可从正式 selection manifest 的本地 qfq 日线构建一次 ``trade_plan``；间隔轮询只
调用一次批量报价，复用已保存计划并运行确定性阈值状态机。外部参考永不进入监控池。
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_KEY = "intraday_decision"
VERSION = "intraday-decision-v1"
ACTION_RANK = {"data_insufficient": -1, "hold": 0, "add": 1, "reduce": 2, "sell": 3}
LOGGER = logging.getLogger(__name__)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _code(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def trading_session(now: datetime | None = None) -> bool:
    """只判断交易时段；交易日历/节假日由 jobs_hub 的既有门禁负责。"""
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    if current.weekday() >= 5:
        return False
    minute = current.hour * 60 + current.minute
    return 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60


def _formal_parts(formal: dict[str, Any]) -> tuple[list, list, dict, dict]:
    artifacts = formal.get("artifacts") or {}
    top15 = (artifacts.get("formal_top15") or {}).get("payload") or []
    top5 = (artifacts.get("formal_top5") or {}).get("payload") or []
    overlay = (artifacts.get("display_overlay") or {}).get("payload") or []
    overlays = {
        _code(row.get("code") or row.get("symbol")): row
        for row in overlay if isinstance(row, dict) and _code(row.get("code") or row.get("symbol"))
    }
    wencai = (artifacts.get("wencai_strategy_runs") or {}).get("payload") or {}
    return list(top15), list(top5), overlays, wencai


def build_monitor_pool(formal: dict[str, Any], holdings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """去重合并持仓、TOP5、TOP15；返回顺序不受问财内容影响。"""
    top15, top5, overlays, _ = _formal_parts(formal or {})
    run_id = str((formal or {}).get("run_id") or "")
    metadata = (formal or {}).get("metadata") or {}
    selection_as_of = metadata.get("decision_at") or (formal or {}).get("selection_date")
    by_code: dict[str, dict[str, Any]] = {}

    for raw in holdings or ():
        if not isinstance(raw, dict):
            continue
        symbol = _code(raw.get("code") or raw.get("symbol"))
        if not symbol:
            continue
        quantity = _finite(raw.get("quantity", raw.get("shares")))
        if quantity is not None and quantity <= 0:
            continue
        by_code[symbol] = {
            "symbol": symbol,
            "name": str(raw.get("name") or ""),
            "sources": ["holding"],
            "priority": "holding",
            "cost_price": _finite(raw.get("cost_price", raw.get("cost"))),
            "quantity": quantity,
            "selection_run_id": run_id,
            "selection_as_of": selection_as_of,
            "formal_rank": None,
        }

    top15_rank = {
        _code(row.get("code") or row.get("symbol")): int(row.get("rank") or index)
        for index, row in enumerate(top15, 1) if isinstance(row, dict)
        and _code(row.get("code") or row.get("symbol"))
    }
    top5_codes = [_code(row.get("code") or row.get("symbol")) for row in top5 if isinstance(row, dict)]
    for raw in top15:
        if not isinstance(raw, dict):
            continue
        symbol = _code(raw.get("code") or raw.get("symbol"))
        if not symbol:
            continue
        overlay = overlays.get(symbol) or {}
        source = "formal_top5" if symbol in top5_codes else "formal_top15_watch"
        row = by_code.setdefault(symbol, {
            "symbol": symbol,
            "name": "",
            "sources": [],
            "priority": source,
            "cost_price": None,
            "quantity": None,
        })
        if source not in row["sources"]:
            row["sources"].append(source)
        row["name"] = row.get("name") or str(overlay.get("name") or raw.get("name") or "")
        row["selection_run_id"] = run_id
        row["selection_as_of"] = selection_as_of
        row["formal_rank"] = top15_rank.get(symbol)
        row["formal_top5_rank"] = (top5_codes.index(symbol) + 1) if symbol in top5_codes else None
        row["technical_state"] = raw.get("technical_state") or overlay.get("technical_state")
        if source == "formal_top5" and row.get("priority") != "holding":
            row["priority"] = source

    priority = {"holding": 0, "formal_top5": 1, "formal_top15_watch": 2}
    return sorted(by_code.values(), key=lambda row: (
        priority.get(str(row.get("priority")), 9), int(row.get("formal_rank") or 9999), row["symbol"]
    ))


def _parse_quote_time(value: Any, now: datetime) -> tuple[datetime, str]:
    text = str(value or "").strip()
    if not text:
        return now, "retrieved_at"
    try:
        if text.isdigit() and len(text) >= 14:
            parsed = datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=SHANGHAI)
            parsed = parsed.astimezone(SHANGHAI)
        return parsed, "provider"
    except (TypeError, ValueError):
        return now, "retrieved_at_invalid_provider_time"


def assess_quotes(pool: Iterable[dict[str, Any]], quotes: dict[str, dict[str, Any]],
                  now: datetime, stale_minutes: int = 8) -> dict[str, Any]:
    pool = list(pool)
    rows: dict[str, dict[str, Any]] = {}
    stale, missing, valid = [], [], []
    as_of_values: list[datetime] = []
    for item in pool:
        symbol = item["symbol"]
        quote = quotes.get(symbol) or quotes.get(str(symbol).zfill(6)) or {}
        price = _finite(quote.get("price")) if isinstance(quote, dict) else None
        raw_stamp = (quote.get("quote_time") or quote.get("as_of")
                     or quote.get("retrieved_at")) if isinstance(quote, dict) else None
        stamp, parsed_source = _parse_quote_time(raw_stamp, now)
        declared_source = str(quote.get("quote_time_source") or "").strip()
        stamp_source = declared_source or parsed_source
        age_minutes = max(0.0, (now - stamp).total_seconds() / 60.0)
        is_stale = bool(raw_stamp) and age_minutes > stale_minutes
        actionable = bool(price and price > 0 and not is_stale)
        if not price or price <= 0:
            missing.append(symbol)
        elif is_stale:
            stale.append(symbol)
        else:
            valid.append(symbol)
            as_of_values.append(stamp)
        rows[symbol] = {
            "price": round(price, 3) if price and price > 0 else None,
            "change_pct": _finite(quote.get("change_pct")) if isinstance(quote, dict) else None,
            "quote_as_of": stamp.isoformat(timespec="seconds"),
            "quote_time_source": stamp_source,
            "quote_provider": quote.get("source") if isinstance(quote, dict) else None,
            "quote_age_minutes": round(age_minutes, 2),
            "price_actionable": actionable,
        }
    requested = len(pool)
    coverage = len(valid) / requested if requested else 1.0
    if requested == 0:
        status = "skipped"
    elif not valid:
        status = "error"
    elif coverage < 0.9 or stale:
        status = "degraded"
    else:
        status = "success"
    missing_asset_types: dict[str, list[str]] = {}
    for symbol in missing:
        asset_type = (
            "fund_or_etf" if symbol.startswith(("15", "16", "18", "50", "51", "52", "56", "58"))
            else "a_share" if symbol[:1] in {"0", "2", "3", "4", "6", "8", "9"}
            else "unsupported_or_unknown"
        )
        missing_asset_types.setdefault(asset_type, []).append(symbol)
    return {
        "status": status,
        "requested": requested,
        "valid": len(valid),
        "coverage": round(coverage, 4),
        "missing_symbols": missing,
        "missing_by_asset_type": missing_asset_types,
        "unsupported_asset_symbols": missing_asset_types.get("unsupported_or_unknown", []),
        "stale_symbols": stale,
        "quote_as_of": min(as_of_values).isoformat(timespec="seconds") if as_of_values else None,
        "items": rows,
    }


def _plans_from_previous(previous: dict[str, Any], run_id: str) -> dict[str, dict[str, Any]]:
    if str(previous.get("selection_run_id") or "") != str(run_id or ""):
        return {}
    plans = previous.get("plans") or {}
    return {str(k): dict(v) for k, v in plans.items() if isinstance(v, dict)}


def _build_missing_plans(formal: dict[str, Any], pool: list[dict[str, Any]],
                         plans: dict[str, dict[str, Any]], snapshot_loader: Callable) -> None:
    manifest_id = str((formal.get("metadata") or {}).get("manifest_id") or "")
    missing = [row["symbol"] for row in pool if row["symbol"] not in plans]
    if not missing or not manifest_id:
        return
    try:
        import pandas as pd

        from analysis.trade_plan import build_trade_plan
        from data.research_store import ResearchStore

        panel = ResearchStore(ensure_schema=False).load_daily_panel_from_manifest(
            manifest_id, symbols=missing
        )
    except Exception:
        LOGGER.exception("intraday plan source unavailable")
        return
    market_signal = snapshot_loader("_market_add_signal") or {}
    for item in pool:
        symbol = item["symbol"]
        if symbol not in missing:
            continue
        try:
            frame = panel[panel["symbol"].astype(str).str[-6:] == symbol].copy()
            basis = "formal_manifest_qfq"
            if frame.empty and "holding" in (item.get("sources") or []):
                # Funds/ETFs and holdings outside the A-share selection universe
                # are absent from the formal manifest. Fixed nodes may reuse the
                # warmed qfq cache; the 20-minute loop never enters this branch.
                import datahub

                frame = datahub.kline(symbol, "1y", "1d", use_cache=True, adjust="qfq")
                quality = datahub.kline_quality(frame)
                if not quality.get("actionable"):
                    plans[symbol] = {
                        "available": False, "action": "hold", "action_cn": "不动",
                        "blockers": [f"持仓 qfq 日 K 不可用：{quality.get('reason') or 'unknown'}"],
                        "price_basis": "fixed_node_warmed_qfq_cache",
                    }
                    continue
                basis = f"fixed_node_warmed_qfq_cache:{quality.get('source') or 'unknown'}"
            if not frame.empty and "trade_date" in frame.columns:
                frame = frame.sort_values("trade_date")
                frame.index = pd.to_datetime(frame["trade_date"], errors="coerce")
            frame = frame.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })
            plan = build_trade_plan(
                symbol, frame, name=item.get("name") or "", market_signal=market_signal,
                technical_state=item.get("technical_state") if isinstance(item.get("technical_state"), dict) else None,
            )
        except Exception as exc:  # noqa: BLE001 - isolate a malformed symbol plan
            plan = {
                "available": False, "action": "hold", "action_cn": "不动",
                "blockers": [f"本地交易计划生成失败：{type(exc).__name__}"],
            }
        plan["price_basis"] = (
            f"trade_plan；{basis}；manifest {manifest_id[:12]}；"
            f"qfq 日线截至 {(formal.get('metadata') or {}).get('market_as_of') or '未知'}"
        )
        plan["plan_as_of"] = (formal.get("metadata") or {}).get("market_as_of")
        plans[symbol] = plan


def _holding_decision(item: dict[str, Any], quote: dict[str, Any], plan: dict[str, Any],
                      snapshot_loader: Callable, fail_closed: bool) -> dict[str, Any]:
    price = quote.get("price")
    if fail_closed or not quote.get("price_actionable"):
        return {"action": "data_insufficient", "action_cn": "数据不足", "reason": "行情缺失或陈旧，暂不给价"}
    snap = snapshot_loader(item["symbol"]) or {}
    cost = item.get("cost_price")
    pnl = round((price - cost) / cost * 100, 2) if price and cost and cost > 0 else None
    ma20 = _finite(snap.get("ma20", snap.get("MA20")))
    ma60 = _finite(snap.get("ma60", snap.get("MA60")))
    score, reasons = 0, []
    if ma60 and price < ma60 * 0.98:
        score += 2; reasons.append(f"跌破60日均线({ma60:.2f})")
    elif ma20 and price < ma20:
        score += 1; reasons.append(f"跌破20日均线({ma20:.2f})")
    if pnl is not None and pnl <= -10:
        score += 1; reasons.append(f"持仓浮亏{pnl:.1f}%")
    if (quote.get("change_pct") or 0) <= -5:
        score += 1; reasons.append(f"盘中下跌{quote.get('change_pct'):.1f}%")
    stop = _finite(plan.get("stop_loss"))
    target = _finite(plan.get("target_price"))
    if stop and price <= stop:
        action, reason = "sell", f"当前价触及 trade_plan 止损 {stop:.2f}"
    elif target and price >= target:
        action, reason = "reduce", f"当前价触及 trade_plan 第一目标 {target:.2f}"
    elif score >= 3:
        action, reason = "sell", "；".join(reasons)
    elif score >= 1:
        action, reason = "reduce", "；".join(reasons)
    else:
        action, reason = "hold", "未触及止损/止盈或技术减仓条件"
    return {
        "action": action,
        "action_cn": {"hold": "不动", "reduce": "减仓", "sell": "卖出"}[action],
        "reason": reason,
        "holding_pnl_pct": pnl,
    }


def _candidate_decision(quote: dict[str, Any], plan: dict[str, Any], fail_closed: bool) -> dict[str, Any]:
    price = quote.get("price")
    if fail_closed or not quote.get("price_actionable"):
        return {"action": "data_insufficient", "action_cn": "数据不足", "reason": "行情缺失或陈旧，暂不给价"}
    if not plan.get("available"):
        return {"action": "data_insufficient", "action_cn": "数据不足", "reason": "缺少有效 trade_plan，暂不给价"}
    low, high = _finite(plan.get("entry_low")), _finite(plan.get("entry_high"))
    stop, target = _finite(plan.get("stop_loss")), _finite(plan.get("target_price"))
    buy_approved = str(plan.get("candidate_action") or plan.get("action") or "") in {"buy", "add"}
    if stop and price <= stop:
        return {"action": "hold", "action_cn": "回避", "reason": f"已触及止损参考价 {stop:.2f}"}
    if target and price >= target:
        return {"action": "hold", "action_cn": "等待", "reason": f"已到第一目标 {target:.2f}，不追高"}
    if buy_approved and low is not None and high is not None and low <= price <= high:
        return {"action": "add", "action_cn": "买入", "reason": f"进入买入区 {low:.2f}-{high:.2f}"}
    trigger = (f"{low:.2f}-{high:.2f}" if low is not None and high is not None else "暂不给价")
    reason = (f"等待，触发价为 {trigger}" if buy_approved else
              f"等待，触发价为 {trigger}；trade_plan 尚未放行买入")
    return {"action": "hold", "action_cn": "等待", "reason": reason}


def _event_state(previous: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = previous.get("trigger_state") or {}
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)}


def _transition(events: list, states: dict, key: str, active: bool, now: datetime,
                payload: dict, cooldown_minutes: int) -> None:
    old = states.get(key) or {}
    was_active = bool(old.get("active"))
    last = old.get("last_alerted_at")
    allowed = True
    if last:
        try:
            allowed = now - datetime.fromisoformat(str(last)) >= timedelta(minutes=cooldown_minutes)
        except (TypeError, ValueError):
            allowed = True
    if active and not was_active and allowed:
        events.append({"state_key": key, **payload})
        old["last_alerted_at"] = now.isoformat(timespec="seconds")
    old["active"] = bool(active)
    states[key] = old


def _fmt_price(value: Any) -> str:
    number = _finite(value)
    return f"¥{number:.2f}" if number is not None else "暂不给价"


def _row_price(row: dict[str, Any], key: str) -> str:
    """不可行动报价下不把历史计划价伪装成本轮可执行价。"""
    if not row.get("price_actionable") or row.get("action") == "data_insufficient":
        return "暂不给价"
    return _fmt_price(row.get(key))


def _decision_row(item: dict[str, Any], quote: dict[str, Any], plan: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    return {
        **item,
        **quote,
        "action": decision.get("action"),
        "action_cn": decision.get("action_cn"),
        "reason": decision.get("reason"),
        "holding_pnl_pct": decision.get("holding_pnl_pct"),
        "entry_low": _finite(plan.get("entry_low")),
        "entry_high": _finite(plan.get("entry_high")),
        "stop_loss": _finite(plan.get("stop_loss")),
        "target_price": _finite(plan.get("target_price")),
        "target_price_2": _finite(plan.get("target_price_2")),
        "price_basis": plan.get("price_basis") or "数据不足，暂不给价",
        "plan_as_of": plan.get("plan_as_of"),
        "plan_available": bool(plan.get("available")),
    }


def format_fixed_summary(snapshot: dict[str, Any], label: str) -> str:
    """QQ 即时报告的八行内摘要；完整明细留在 Agent 快照。"""
    quality = snapshot.get("data_quality") or {}
    as_of = quality.get("quote_as_of") or snapshot.get("generated_at") or "未知"
    lines = [
        (
            f"{label}盘中决策｜行情{quality.get('valid', 0)}/{quality.get('requested', 0)} "
            f"({quality.get('coverage', 0) * 100:.0f}%)｜计划{quality.get('plan_available', 0)}/"
            f"{quality.get('plan_requested', 0)}｜as-of {str(as_of)[11:19] or as_of}"
        ),
    ]
    holdings = snapshot.get("holdings") or []
    candidates = (snapshot.get("formal_top5") or []) + (snapshot.get("formal_top15_watch") or [])
    lines.append(f"持仓 {len(holdings)} 只（完整覆盖见 intraday_decision_snapshot）")
    shown_h = sorted(holdings, key=lambda x: -ACTION_RANK.get(str(x.get("action")), -1))[:2]
    for row in shown_h:
        lines.append(
            f"持仓 {row.get('name') or row['symbol']}({_code(row['symbol'])}) 当前{_row_price(row, 'price')}｜"
            f"{row.get('action_cn')}｜卖出/止损{_row_price(row, 'stop_loss')}｜"
            f"止盈{_row_price(row, 'target_price')}｜{str(row.get('quote_as_of') or '')[11:19]}"
        )
    independent = snapshot.get("independent_selection") or {}
    independent_label = (str(len(independent.get("top5") or []))
                         if independent.get("status") == "ready" else "不可用")
    lines.append(
        f"正式候选 TOP5 {len(snapshot.get('formal_top5') or [])} / "
        f"观察 {len(snapshot.get('formal_top15_watch') or [])}｜独立TOP5 {independent_label}"
    )
    shown_c = (snapshot.get("formal_top5") or [])[:2]
    if not shown_c:
        shown_c = candidates[:2]
    for row in shown_c:
        entry = (f"¥{row['entry_low']:.2f}-{row['entry_high']:.2f}"
                 if row.get("entry_low") is not None and row.get("entry_high") is not None else "暂不给价")
        if not row.get("price_actionable") or row.get("action") == "data_insufficient":
            entry = "暂不给价"
        lines.append(
            f"候选 {row.get('name') or row['symbol']}({_code(row['symbol'])}) 当前{_row_price(row, 'price')}｜"
            f"{row.get('action_cn')}｜买入{entry}｜止损{_row_price(row, 'stop_loss')}｜"
            f"目标{_row_price(row, 'target_price')}｜{str(row.get('quote_as_of') or '')[11:19]}"
        )
    if quality.get("status") != "success":
        lines.append("⚠️ 行情缺失、陈旧或计划不足项已失败关闭，不据此给明确动作")
    lines.append("价格依据：trade_plan + 正式 manifest qfq 日线；仅研究建议，不自动下单")
    return "\n".join(lines[:8])


def format_alert(event: dict[str, Any], snapshot: dict[str, Any]) -> tuple[str, str]:
    kind = event.get("trigger_type")
    labels = {
        "entry": "进入买入区", "stop": "触及止损", "target": "触及止盈",
        "action_escalation": "持仓动作升级", "quote_degraded": "盘中关键数据降级",
    }
    title = f"盘中提醒：{labels.get(kind, kind)}"
    if kind == "quote_degraded":
        q = snapshot.get("data_quality") or {}
        return title, (
            f"行情覆盖 {q.get('valid')}/{q.get('requested')}（{q.get('coverage', 0) * 100:.0f}%）\n"
            f"价格计划 {q.get('plan_available', 0)}/{q.get('plan_requested', 0)}\n"
            f"缺失:{','.join(q.get('missing_symbols') or []) or '无'}\n"
            f"陈旧:{','.join(q.get('stale_symbols') or []) or '无'}\n"
            f"计划不足:{','.join(q.get('plan_missing_symbols') or []) or '无'}\n"
            "无效项已失败关闭：本轮不据此给明确买卖价"
        )
    row = event.get("item") or {}
    entry = (f"¥{row['entry_low']:.2f}-{row['entry_high']:.2f}"
             if row.get("entry_low") is not None and row.get("entry_high") is not None else "暂不给价")
    body = [
        f"{row.get('name') or row.get('symbol')}({row.get('symbol')}) 当前{_fmt_price(row.get('price'))}",
        f"动作：{row.get('action_cn')}｜买入区{entry}",
        f"止损：{_fmt_price(row.get('stop_loss'))}｜第一目标：{_fmt_price(row.get('target_price'))}",
        f"依据：{row.get('reason') or row.get('price_basis')}",
        f"数据时点：{row.get('quote_as_of') or '未知'}",
        "仅研究建议，不自动下单",
    ]
    return title, "\n".join(body)


def run_cycle(*, now: datetime | None = None, allow_plan_build: bool = False,
              notify_changes: bool = True, quote_loader: Callable | None = None,
              formal_loader: Callable | None = None, holdings_loader: Callable | None = None,
              snapshot_loader: Callable | None = None, snapshot_saver: Callable | None = None,
              notify_fn: Callable | None = None,
              holding_overrides: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    current = current.astimezone(SHANGHAI)
    if not trading_session(current):
        return {"status": "skipped", "reason": "outside_a_share_trading_session"}

    if formal_loader is None:
        from data.research_store import ResearchStore
        formal_loader = lambda: ResearchStore(ensure_schema=False).latest_formal_selection() or {}
    if holdings_loader is None:
        from portfolio_db import portfolio_db
        holdings_loader = lambda: portfolio_db.get_all_stocks() or []
    if quote_loader is None:
        import datahub
        quote_loader = datahub.quotes
    if snapshot_loader is None or snapshot_saver is None:
        from jobs import jobs_hub
        snapshot_loader = snapshot_loader or jobs_hub.get_indicator_snapshot
        snapshot_saver = snapshot_saver or jobs_hub.save_indicator_snapshot

    formal = formal_loader() or {}
    if str(formal.get("selection_date") or "")[:10] != current.date().isoformat():
        return {"status": "skipped", "reason": "today_formal_selection_unavailable"}
    pool = build_monitor_pool(formal, holdings_loader() or [])
    previous = snapshot_loader(SNAPSHOT_KEY) or {}
    plans = _plans_from_previous(previous, str(formal.get("run_id") or ""))
    if allow_plan_build:
        _build_missing_plans(formal, pool, plans, snapshot_loader)
    if not allow_plan_build and any(row["symbol"] not in plans for row in pool):
        return {"status": "skipped", "reason": "awaiting_fixed_node_trade_plans"}

    # 一个调用覆盖全池；此后不逐股回退。
    quotes = quote_loader([row["symbol"] for row in pool]) or {}
    stale_minutes = max(1, int(os.getenv("INTRADAY_QUOTE_STALE_MINUTES", "8")))
    quality = assess_quotes(pool, quotes, current, stale_minutes=stale_minutes)
    plan_missing = [row["symbol"] for row in pool if not (plans.get(row["symbol"]) or {}).get("available")]
    quality["plan_requested"] = len(pool)
    quality["plan_available"] = len(pool) - len(plan_missing)
    quality["plan_coverage"] = round((len(pool) - len(plan_missing)) / len(pool), 4) if pool else 1.0
    quality["plan_missing_symbols"] = plan_missing
    if plan_missing and quality["status"] == "success":
        quality["status"] = "degraded"
    critical_coverage = float(os.getenv("INTRADAY_CRITICAL_QUOTE_COVERAGE", "0.70"))
    fail_closed = quality["status"] == "error" or quality["coverage"] < critical_coverage
    states = _event_state(previous)
    cooldown = max(60, int(os.getenv("INTRADAY_ALERT_COOLDOWN_MINUTES", "60")))
    events: list[dict[str, Any]] = []
    decisions: dict[str, dict[str, Any]] = {}

    for item in pool:
        symbol = item["symbol"]
        quote = quality["items"].get(symbol) or {}
        plan = plans.get(symbol) or {"available": False}
        is_holding = "holding" in (item.get("sources") or [])
        override = (holding_overrides or {}).get(symbol) if is_holding else None
        row_fail_closed = fail_closed or not plan.get("available")
        decision = (dict(override) if isinstance(override, dict) and not row_fail_closed
                    and quote.get("price_actionable")
                    else _holding_decision(item, quote, plan, snapshot_loader, row_fail_closed)
                    if is_holding else _candidate_decision(quote, plan, row_fail_closed))
        row = _decision_row(item, quote, plan, decision)
        decisions[symbol] = row
        actionable = bool(quote.get("price_actionable")) and not fail_closed
        price = quote.get("price")
        entry = (actionable and not is_holding and decision.get("action") == "add")
        stop = actionable and bool(row.get("stop_loss")) and price <= row["stop_loss"]
        target = actionable and bool(row.get("target_price")) and price >= row["target_price"]
        for kind, active in (("entry", entry), ("stop", stop), ("target", target)):
            _transition(events, states, f"{symbol}:{kind}", bool(active), current,
                        {"trigger_type": kind, "symbol": symbol, "item": row}, cooldown)
        old_rank = int((states.get(f"{symbol}:action") or {}).get("rank", 0))
        new_rank = ACTION_RANK.get(str(row.get("action")), -1)
        for level in ("reduce", "sell"):
            escalation = (actionable and row.get("action") == level
                          and new_rank > old_rank)
            _transition(events, states, f"{symbol}:action_escalation:{level}", escalation,
                        current, {"trigger_type": "action_escalation", "symbol": symbol,
                                  "item": row}, cooldown)
        action_state = states.setdefault(f"{symbol}:action", {})
        action_state["rank"] = new_rank
        action_state["active"] = new_rank >= ACTION_RANK["reduce"]

    degraded = quality["status"] in {"degraded", "error"}
    _transition(events, states, "__pool__:quote_degraded", degraded, current,
                {"trigger_type": "quote_degraded", "symbol": "__pool__"}, cooldown)
    formal_top15, _, _, wencai = _formal_parts(formal)
    artifacts = formal.get("artifacts") or {}
    from analysis.independent_selector import (
        artifact_payload, comparison as compare_selection_lanes,
    )
    independent_selection = artifact_payload(artifacts)
    selection_comparison = compare_selection_lanes(
        formal_top15, independent_selection, wencai
    )
    snapshot = {
        "version": VERSION,
        "trade_date": current.date().isoformat(),
        "generated_at": current.isoformat(timespec="seconds"),
        "status": quality["status"],
        "selection_run_id": formal.get("run_id"),
        "selection_as_of": (formal.get("metadata") or {}).get("decision_at") or formal.get("selection_date"),
        "data_quality": {key: value for key, value in quality.items() if key != "items"},
        "holdings": [decisions[row["symbol"]] for row in pool if "holding" in row.get("sources", [])],
        "formal_top5": [decisions[row["symbol"]] for row in pool if "formal_top5" in row.get("sources", [])],
        "formal_top15_watch": [decisions[row["symbol"]] for row in pool if "formal_top15_watch" in row.get("sources", [])],
        "wencai_reference": wencai,
        "independent_selection": independent_selection,
        "selection_comparison": selection_comparison,
        "monitor_pool": pool,
        "plans": plans,
        "trigger_state": states,
        "events": [{key: value for key, value in event.items() if key != "item"} for event in events],
        "execution_boundary": "research_only_no_broker_no_auto_order",
    }
    snapshot_saver(SNAPSHOT_KEY, snapshot)

    if notify_changes and events:
        if notify_fn is None:
            from notify.notification_router import send
            notify_fn = lambda title, body: send("alert", title, body)
        for event in events:
            title, body = format_alert(event, snapshot)
            try:
                notify_fn(title, body)
            except Exception:
                LOGGER.exception("intraday transition notification failed: %s", event.get("state_key"))
    return snapshot


def latest_snapshot(snapshot_loader: Callable | None = None) -> dict[str, Any]:
    if snapshot_loader is None:
        from jobs.jobs_hub import get_indicator_snapshot
        snapshot_loader = get_indicator_snapshot
    value = snapshot_loader(SNAPSHOT_KEY) or {}
    today = datetime.now(SHANGHAI).date().isoformat()
    if value.get("trade_date") != today:
        return {"status": "missing", "reason": "today_intraday_snapshot_unavailable", "data": None}
    return {"status": value.get("status") or "success", "data": value}
