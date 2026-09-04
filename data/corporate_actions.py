"""Bounded dividend adapter. Dividend endpoint coverage is NOT all-action coverage."""
from application.results import payload_hash


def normalize_dividends(rows, symbol, *, start, end, observed_at):
    events, gaps = [], []
    for row in rows:
        ex = str(row.get("ex_date") or "")
        ex = f"{ex[:4]}-{ex[4:6]}-{ex[6:8]}" if len(ex) == 8 else ex
        if not start <= ex <= end:
            continue
        if row.get("div_proc") != "实施":
            continue
        # cash_div_tax is gross and cash_div may not represent the holder's final
        # tax. Keep source facts, never silently invent net entitlement or quantity.
        event = {"symbol": symbol, "ex_date": ex, "record_date": row.get("record_date"),
                 "payment_date": row.get("pay_date"), "gross_cash_per_share": row.get("cash_div_tax"),
                 "reported_cash_per_share": row.get("cash_div"), "stock_div": row.get("stk_div"),
                 "provider": "tushare", "observed_at": observed_at, "status": "requires_entitlement_and_tax"}
        event["event_id"] = payload_hash({k: v for k, v in event.items() if k != "observed_at"})
        events.append(event)
        gaps.append({"event_id": event["event_id"], "reason": "net_entitlement_unconfirmed"})
    return {"symbol": symbol, "start": start, "end": end, "status": "SUCCESS" if events else "EMPTY",
            "events": events, "gaps": gaps, "scope": ["dividends"], "pagination_complete": False,
            "corporate_actions_complete": False, "reason": "dividend_endpoint_is_not_all_actions"}


def fetch_dividends(symbol, *, start, end, observed_at):
    from data.sources.tushare import _pro, _ts_code
    from data.source_contracts import source_call
    api = _pro()
    if api is None:
        return {"symbol": symbol, "status": "UNAVAILABLE", "corporate_actions_complete": False}
    with source_call("tushare", "dividend"):
        frame = api.dividend(ts_code=_ts_code(symbol), fields="ts_code,div_proc,record_date,ex_date,pay_date,cash_div,cash_div_tax,stk_div")
    return normalize_dividends(frame.to_dict("records"), symbol, start=start, end=end, observed_at=observed_at)


def coverage_complete(receipt, *, symbol, day, events):
    return bool(receipt and receipt.get("symbol") == symbol and receipt.get("start", "9999") <= day
                <= receipt.get("end", "0000") and receipt.get("status") in {"SUCCESS", "EMPTY"}
                and receipt.get("pagination_complete") and receipt.get("source_evidence")
                and set(receipt.get("scope", [])) >= {"dividends", "splits", "rights", "reorganizations", "delistings"}
                and not receipt.get("gaps") and all(e.get("settlement_verified") for e in events))
