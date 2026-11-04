"""Equity-only daily exit review. No trading and no LLM dependency."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from data.reliability_store import ReliabilityStore

TZ = ZoneInfo("Asia/Shanghai")
LIMIT = 15


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


def build_daily(holdings, metadata, decisions, *, now, market_date=None):
    counts = count_positions(holdings, metadata)
    lines = [f"个股 {counts['equity_count']}/{LIMIT}，超 {counts['excess']} 只｜ETF {counts['etf_count']} 只另计",
             f"其他场内基金 {counts['other_fund_count']} 只｜数量或品种待核 {counts['unknown_count']} 项"]
    ranked = sorted((r for r in decisions if str(r.get("symbol") or r.get("code")) in counts["equity_symbols"]),
                    key=lambda r: ({"sell": 0, "reduce": 1}.get(r.get("action"), 2),
                                   -float(r.get("exit_score") or 0), str(r.get("symbol") or r.get("code"))))
    reviews = [r for r in ranked if r.get("action") in {"sell", "reduce"}
               or r.get("requested_action") in {"sell", "reduce"}]
    for row in reviews[:3]:
        symbol = str(row.get("symbol") or row.get("code"))
        action = row.get("action")
        label = "退出复核" if action == "sell" else "部分减仓复核" if action == "reduce" else "风险观察"
        reason = str(row.get("reason") or row.get("requested_reason") or row.get("rule_reason") or "原风险触发待核")
        lines.append(f"{label}｜{symbol} {str(row.get('name') or '')[:20]}｜{reason[:65]}")
    if not reviews:
        lines.append("今日无证据充分的整只退出候选。先核持仓、报价和原计划失效条件。")
    lines.append("整只可卖量待核；部分减仓释放0个名额，实际清仓成交入账后才扣只数。")
    lines.append(f"账本读取 {now.strftime('%m-%d %H:%M')}｜最近行情 {market_date or '待核'}｜下次开市复核可卖量和报价")
    return {"counts": counts, "items": reviews, "text": "\n".join(lines),
            "required": counts["equity_count"] > LIMIT,
            "needs_review": counts["unknown_count"] > 0,
            "created_at": now.isoformat(), "auto_execution": False}


class DailyExitService:
    def __init__(self, store):
        self.repo = ReliabilityStore(store)

    def deliver(self, report, sender, *, owner="primary"):
        if not report["required"] and not report["needs_review"]:
            return {"status": "not_required"}
        day = datetime.fromisoformat(report["created_at"]).astimezone(TZ).date().isoformat()
        kind = "daily_equity_exit" if report["required"] else "daily_equity_count_review"
        from application.results import payload_hash
        key = f"{kind}:{day}:{payload_hash(owner)[:12]}"
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
                report = old["report"]
                kwargs["only_channels"] = [k for k, v in previous_receipts.items() if not v[0]]
            record = self.repo.append(cur, kind, key, owner,
                                      {"status": "unknown", "report": report, "attempt": attempt,
                                       "receipts": previous_receipts}, old)
        try:
            result = sender("每日清仓建议" if report["required"] else "个股只数待核",
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
            receipts, status, archive = {}, "unknown", "unrecorded"
        return self.repo.put(kind, key, {**record, "status": status,
                            "receipts": receipts, "archive_status": archive},
                            owner=owner, expected_revision=record["revision"])


def run_daily_exit():
    from portfolio_db import portfolio_db
    from data.research_store import ResearchStore
    from application.stock_budget import load_security_metadata
    from notify.notification_router import send
    from jobs.jobs_hub import get_indicator_snapshot
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
        return DailyExitService(store).deliver(report, send)
    metadata = load_security_metadata(store, now.date().isoformat())
    snapshot = get_indicator_snapshot("intraday_decision") or {}
    # Old snapshots are useful observations, never today's executable orders.
    decisions = snapshot.get("holdings") or []
    report = build_daily(context["holdings"], metadata, decisions, now=now,
                         market_date=str(snapshot.get("generated_at") or "")[:10] or None)
    report["holdings_watermark"] = context.get("watermark")
    return DailyExitService(store).deliver(report, send)
