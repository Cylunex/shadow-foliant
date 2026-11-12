"""Equity-only daily exit review. No trading and no LLM dependency."""
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from application.market_prices import shanghai_instant
from application.results import payload_hash
from data.reliability_store import ReliabilityStore
from application.holding_review import ordered_holdings, risk_label

TZ = ZoneInfo("Asia/Shanghai")
LIMIT = 15
FINAL_ACTIONS = {"sell": "退出复核", "reduce": "部分减仓复核", "hold": "不动", "add": "加仓复核"}


def count_positions(holdings, metadata):
    stocks = set(metadata.get("stock_symbols") or ())
    etfs = set(metadata.get("etf_symbols") or ())
    funds = set(metadata.get("fund_symbols") or ())
    grouped = {}
    unknown = set()
    for row in holdings:
        symbol = str(row.get("symbol") or row.get("code") or "").zfill(6)
        try:
            quantity = Decimal(str(row.get("quantity")))
            if not quantity.is_finite() or quantity < 0:
                raise ValueError()
        except (InvalidOperation, ValueError, TypeError):
            unknown.add(symbol)
            continue
        grouped[symbol] = grouped.get(symbol, Decimal(0)) + quantity
    active = {s for s, q in grouped.items() if q > 0}
    equities = active & stocks
    etf = (active - stocks) & etfs
    other = (active - stocks - etf) & funds
    unknown |= active - equities - etf - other
    return dict(equity_count=len(equities), etf_count=len(etf),
                other_fund_count=len(other), unknown_count=len(unknown),
                equity_symbols=sorted(equities), excess=max(0, len(equities) - LIMIT))


def load_account_counts(holdings):
    from data.research_store import ResearchStore
    from application.stock_budget import load_security_metadata
    metadata = load_security_metadata(ResearchStore(ensure_schema=False))
    return count_positions(holdings, metadata)


def position_count_line(counts, limit=LIMIT):
    return (f"个股 {counts['equity_count']}/{limit}｜ETF {counts['etf_count']} 只另计｜"
            f"其他基金 {counts['other_fund_count']}｜待核 {counts['unknown_count']}")


def _observations(rows, cutoff):
    """Freeze only final decisions with usable provider quotes from this report day."""
    observations = {}
    for row in rows:
        quote_at = shanghai_instant(row.get("quote_as_of"))
        if (row.get("action") not in FINAL_ACTIONS or row.get("price_usable") is not True
                or row.get("quote_time_source") != "provider" or not quote_at
                or quote_at.date() != cutoff.date() or quote_at > cutoff):
            continue
        symbol = str(row.get("symbol") or row.get("code"))
        observations[symbol] = {"action": row["action"], "quote_at": quote_at.isoformat(),
                                "reason": " ".join(str(row.get("reason") or "").split())[:55]}
    return observations


def _comparison_line(current, prior, now):
    baseline = shanghai_instant((prior or {}).get("created_at"))
    if not baseline or not 0 < (now.date() - baseline.date()).days <= 10:
        return "判断变化｜尚无有效的前次早盘记录，本次开始积累。"
    previous_label = "昨日" if baseline.date() == now.date() - timedelta(days=1) else f"上次{baseline:%m-%d} "
    previous = prior.get("judgments")
    if previous is None:  # Earlier reports archived review candidates only.
        previous = _observations(prior.get("items") or [], baseline)
    common = [symbol for symbol in current if symbol in previous]
    if not common:
        return "判断变化｜本次或前次有效行情不足，暂不比较动作变化。"
    for symbol in common:  # Current final-risk priority, independent of the raw trigger.
        before, after = previous[symbol], current[symbol]
        if before["action"] != after["action"]:
            return (f"判断变化｜{symbol} {previous_label}{before['quote_at'][11:16]} {FINAL_ACTIONS[before['action']]}"
                    f" → 今日{after['quote_at'][11:16]} {FINAL_ACTIONS[after['action']]}｜"
                    f"当期依据：{after['reason'] or '待核'}")
    return f"判断变化｜两期可比较的 {len(common)} 只持仓，最终判断相同。"


def build_daily(holdings, metadata, decisions, *, now, market_date=None, snapshot_at=None, prior=None):
    counts = count_positions(holdings, metadata)
    lines = [f"个股 {counts['equity_count']}/{LIMIT}，超 {counts['excess']} 只｜ETF {counts['etf_count']} 只另计",
             f"其他场内基金 {counts['other_fund_count']} 只｜数量或品种待核 {counts['unknown_count']} 项"]
    ranked = ordered_holdings(r for r in decisions
                              if str(r.get("symbol") or r.get("code")) in counts["equity_symbols"])
    cutoff = shanghai_instant(snapshot_at)
    local_now = shanghai_instant(now)
    fresh = bool(cutoff and cutoff.date() == local_now.date() and cutoff <= local_now)
    if not fresh:
        ranked = []
    reviews = [r for r in ranked if r.get("action") in {"sell", "reduce"}
               or r.get("requested_action") in {"sell", "reduce"}]
    # generated_at is the collection START. Provider timestamps may legitimately
    # be newer than it; only the report's actual read time is the upper bound.
    judgments = _observations(ranked, local_now) if fresh else {}
    if counts["excess"]:
        lines.insert(1, _comparison_line(judgments, prior, local_now))
    for row in reviews[:3]:
        symbol = str(row.get("symbol") or row.get("code"))
        label = risk_label(row)
        reason = str(row.get("reason") or row.get("requested_reason") or row.get("rule_reason") or "原风险触发待核")
        name = " ".join(str(row.get("name") or "").split())[:20]
        lines.append(f"{label}｜{symbol} {name}｜{' '.join(reason.split())[:65]}")
    if not reviews:
        lines.append("今日无证据充分的整只退出候选。先核持仓、报价和原计划失效条件。")
    lines.append("整只可卖量待核；部分减仓释放0个名额，实际清仓成交入账后才扣只数。")
    lines.append(f"账本读取 {now.strftime('%m-%d %H:%M')}｜最近行情 {market_date or '待核'}｜操作前复核可卖量和实时报价")
    frozen = {symbol: {"action": row["action"], "quote_at": row["quote_at"]} for symbol, row in judgments.items()}
    return {"counts": counts, "items": reviews, "judgments": frozen, "text": "\n".join(lines),
            "required": counts["equity_count"] > LIMIT,
            "needs_review": counts["unknown_count"] > 0,
            "created_at": now.isoformat(), "auto_execution": False}


class DailyExitService:
    def __init__(self, store):
        self.repo = ReliabilityStore(store)

    def yesterday_accepted_report(self, now, *, owner="primary"):
        day = shanghai_instant(now).date() - timedelta(days=1)
        record = self.repo.get("daily_equity_exit", f"daily_equity_exit:{day}:{payload_hash(owner)[:12]}", owner)
        if record and any(receipt[0] for receipt in record.get("receipts", {}).values()):
            return record["report"]
        return None

    def previous_accepted_report(self, now, *, owner="primary"):
        fallback = None
        for record in self.repo.list("daily_equity_exit", owner=owner, limit=20):
            report = record.get("report") or {}
            stamp = shanghai_instant(report.get("created_at"))
            if (stamp and 0 < (now.date() - stamp.date()).days <= 10
                    and any(receipt[0] for receipt in record.get("receipts", {}).values())):
                if report.get("judgments") or _observations(report.get("items") or [], stamp):
                    return report
                fallback = fallback or report
        return fallback

    def deliver(self, report, sender, *, owner="primary"):
        day = datetime.fromisoformat(report["created_at"]).astimezone(TZ).date().isoformat()
        kind = "daily_equity_exit" if report["required"] else "daily_equity_count_review"
        key = f"{kind}:{day}:{payload_hash(owner)[:12]}"
        if not report["required"] and not report["needs_review"]:
            # Close an explicitly rejected obligation against fresh holdings.
            prior_key = f"daily_equity_exit:{day}:{payload_hash(owner)[:12]}"
            with self.repo.transaction() as cur:
                old = self.repo.read(cur, "daily_equity_exit", prior_key, owner)
                if old and old["status"] == "retryable" and not any(v[0] for v in old.get("receipts", {}).values()):
                    self.repo.append(cur, "daily_equity_exit", prior_key, owner,
                                     {**old, "status": "closed", "closed_report": report}, old)
            return {"status": "not_required"}
        # Persist before external I/O. An interrupted/unknown attempt is never
        # blindly repeated, even if the producer restarts or the account changes.
        with self.repo.transaction() as cur:
            old = self.repo.read(cur, kind, key, owner)
            if old and (old["status"] != "retryable" or old.get("attempt", 1) >= 3):
                return old
            attempt = (old or {}).get("attempt", 0) + 1
            previous_receipts = (old or {}).get("receipts", {})
            kwargs = {}
            if old:
                # Once any channel accepted, every remaining channel must see
                # the same immutable body. Otherwise refresh stale holdings.
                if any(v[0] for v in previous_receipts.values()):
                    report = old["report"]
                kwargs["only_channels"] = [k for k, v in previous_receipts.items() if not v[0]]
            record = self.repo.append(cur, kind, key, owner,
                                      {"status": "unknown", "report": report, "attempt": attempt,
                                       "receipts": previous_receipts}, old)
        try:
            result = sender("早盘清仓提醒与候选计划" if report["required"] else "个股只数待核",
                            report["text"], idempotency_key=f"{key}:attempt:{attempt}",
                            source="application.portfolio_daily", **kwargs)
            receipts = {**previous_receipts, **{k: list(v) for k, v in result.items()}}
            failures = [v for v in receipts.values() if not v[0]]
            rejected = failures and all(str(v[1]).startswith(("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404", "HTTP 422", "HTTP 429")) or "app:rejected" in str(v[1]) for v in failures)
            if receipts and not failures:
                status = "accepted"
            elif rejected:
                status = "retryable"
            elif any(v[0] for v in receipts.values()):
                status = "partial"
            else:
                status = "unknown"
            archive = getattr(result, "archive_status", "unrecorded")
        except Exception:
            receipts, status, archive = previous_receipts, "unknown", "unrecorded"
        return self.repo.put(kind, key, {**record, "status": status,
                            "receipts": receipts, "archive_status": archive},
                            owner=owner, expected_revision=record["revision"])


def run_daily_exit(*, snapshot=None):
    from portfolio_db import portfolio_db
    from data.research_store import ResearchStore
    from application.stock_budget import load_security_metadata
    from notify.notification_router import send
    from jobs.jobs_hub import get_indicator_snapshot
    def sender(title, text, **kwargs):
        return send("report", title, text, **kwargs)
    now = datetime.now(TZ)
    store = ResearchStore(ensure_schema=False)
    try:
        context = portfolio_db.action_preview_context(include_unknown=True)
    except Exception:
        prior = ReliabilityStore(store).list("daily_equity_exit", owner="primary", limit=1)
        if not prior:
            raise
        last = prior[0]["report"]
        report = {**last, "created_at": now.isoformat(), "required": False, "needs_review": True,
                  "items": [],
                  "text": f"今日持仓账本读取失败；上次核实 {last['created_at'][:10]} 个股 {last['counts']['equity_count']} 只。\n"
                          "上次个股超限，今日只数与清仓顺序待核。\n"
                          "账本恢复后会重算当日清仓建议；未核实的旧动作只供历史查询。"}
        return DailyExitService(store).deliver(report, sender)
    metadata = load_security_metadata(store, now.date().isoformat())
    snapshot = snapshot if snapshot is not None else (get_indicator_snapshot("intraday_decision") or {})
    stamp = shanghai_instant(snapshot.get("generated_at"))
    if not stamp or stamp.date() != now.date() or not 0 <= (now - stamp).total_seconds() <= 600:
        return {"status": "waiting_snapshot"}
    decisions = snapshot.get("holdings") or []
    service = DailyExitService(store)
    report = build_daily(context["holdings"], metadata, decisions, now=now,
                         prior=service.previous_accepted_report(now), snapshot_at=snapshot.get("generated_at"),
                         market_date=str(snapshot.get("generated_at") or "")[:10] or None)
    report["holdings_watermark"] = context.get("watermark")
    if report["required"]:
        from notify.portfolio_report import render
        report["text"] = render(snapshot, "10:05", exit_review=report)
    return service.deliver(report, sender)
