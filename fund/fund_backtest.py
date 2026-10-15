"""Frozen, budgeted NAV execution simulator. No online reads or real ledger writes."""

from __future__ import annotations
from datetime import timedelta
import pandas as pd
import numpy as np
from fund.fund_calendar import executions
from fund.fund_contracts import (
    VERSION,
    audit_nav,
    decimal,
    fingerprint,
    instant,
    number,
)
from fund.fund_returns import return_frame


def simulate(
    frame,
    amount,
    period="monthly",
    day=1,
    fee_rate=0.0015,
    start=None,
    end=None,
    strategy="normal",
    valuation_series=None,
    *,
    anchor_date=None,
    total_budget=None,
    max_period_amount=None,
    fee_basis="gross",
    dividend_mode="cash",
    redemption_fee=0.0,
    redemption_fee_schedule=None,
    open_dates=None,
    _lump_once=False,
):
    amount = number(amount, positive=True)
    fee_rate, redemption_fee = number(fee_rate), number(redemption_fee)
    if not 0 <= fee_rate < 1 or not 0 <= redemption_fee < 1:
        raise ValueError("无效费率")
    if strategy not in ("normal", "price_position", "valuation", "value_avg"):
        raise ValueError("无效策略")
    if fee_basis not in ("gross", "net") or dividend_mode not in ("cash", "reinvest"):
        raise ValueError("无效费用/分红规则")
    quality = audit_nav(frame, start, end)
    if quality["status"] != "ready":
        return {"error": "净值覆盖不完整", "status": "partial", "quality": quality}
    f = return_frame(frame)
    if f.attrs.get("actions_status") not in (None, "ready"):
        return {"error": "权益事件尚未完整，不能复刻份额回测", "status": "partial"}
    if f.attrs["return_basis"] == "disclosed_total_return":
        return {
            "error": "缺少分红拆分明细，不能仅用收益率模拟份额",
            "status": "partial",
        }
    if start:
        f = f[f.date >= pd.Timestamp(start)]
    if end:
        f = f[f.date <= pd.Timestamp(end)]
    if len(f) < 2:
        return {"error": "净值数据不足", "status": "partial"}
    f = f.set_index("date")
    calendar = open_dates if open_dates is not None else [d.date() for d in f.index]
    schedule = executions(
        f.index[0].date(), f.index[-1].date(), calendar, period, day, anchor_date
    )
    if _lump_once:
        first = str(f.index[0].date())
        if open_dates is not None:
            first = next(
                (
                    str(d)[:10]
                    for d in sorted(open_dates)
                    if first <= str(d)[:10] <= str(f.index[-1].date())
                ),
                None,
            )
        schedule = [{"scheduled_date": first, "execution_date": first}] if first else []
    if not schedule:
        return {"error": "区间内无到期计划", "status": "partial"}
    if any(pd.Timestamp(r["execution_date"]) not in f.index for r in schedule):
        return {
            "error": "执行日缺少对应净值，不按相邻观察日代替成交",
            "status": "partial",
        }
    due = {}
    for item in schedule:
        due.setdefault(item["execution_date"], []).append(item)
    # Equal cash arrival for every strategy; unspent money remains in the portfolio.
    budget = number(
        total_budget if total_budget is not None else amount * len(schedule),
        positive=True,
    )
    max_buy = number(
        max_period_amount if max_period_amount is not None else amount * 2,
        positive=True,
    )
    valuations = None
    if strategy == "valuation":
        if (
            valuation_series is None
            or not isinstance(valuation_series, pd.DataFrame)
            or not {"known_at", "percentile"}.issubset(valuation_series)
        ):
            return {
                "error": "真实估值策略需要 known_at 与 percentile，不使用净值冒充估值",
                "status": "unsupported",
            }
        valuations = valuation_series.copy()
        valuations["_known"] = valuations.known_at.map(instant)
        valuations = valuations.sort_values("_known")
        if not valuations.percentile.map(lambda p: 0 <= number(p) <= 100).all():
            raise ValueError("无效估值分位")
    position = (
        f["total_return_nav"]
        .shift(1)
        .rolling(504, min_periods=20)
        .apply(lambda x: (x <= x[-1]).mean() * 100, raw=True)
    )
    shares = cash = contributed = spent = fees = units = 0.0
    previous = 0.0
    neutral = 1.0
    trades, curve, cashflows, receivables, lots = [], [], [], [], []
    events = frame.attrs.get("actions", {}).get("events", [])
    for d, row in f.iterrows():
        key, nav = d.strftime("%Y-%m-%d"), float(row.unit_nav)
        # Existing shares receive the event; today's new subscription does not.
        cash_div = shares * float(row.cash_dividend)
        shares *= float(row.split_ratio)
        for lot in lots:
            lot["shares"] *= float(row.split_ratio)
        if cash_div:
            pay = next(
                (
                    e.get("payment_date")
                    for e in events
                    if e["date"] == key and e["kind"] == "dividend"
                ),
                None,
            )
            receivables.append({"amount": cash_div, "pay_date": pay})
        released = sum(
            r["amount"] for r in receivables if r["pay_date"] and r["pay_date"] <= key
        )
        receivables = [
            r for r in receivables if not r["pay_date"] or r["pay_date"] > key
        ]
        cash += released
        if released and dividend_mode == "reinvest":
            sh = float(decimal(released / nav, "0.0001"))
            shares += sh
            cash -= sh * nav
            lots.append({"date": key, "shares": sh})
        inflow = min(amount * len(due.get(key, [])), max(0, budget - contributed))
        if inflow:
            cash += inflow
            contributed += inflow
            cashflows.append((d, -inflow))
        target = amount
        if key in due:
            mult = 1.0
            if strategy in ("valuation", "price_position"):
                p = None
                if strategy == "price_position" and pd.notna(position.loc[d]):
                    p = float(position.loc[d])
                if strategy == "valuation":
                    cutoff = instant(key + "T15:00:00+08:00")
                    visible = valuations[valuations._known <= cutoff]
                    if not visible.empty:
                        p = float(visible.iloc[-1].percentile)
                    else:
                        return {
                            "error": "执行日前无可见估值",
                            "status": "partial",
                            "date": key,
                        }
                from fund_valuation import valuation_multiplier

                mult = valuation_multiplier(p) if p is not None else 1.0
                target = amount * mult
            elif strategy == "value_avg":
                target = max(0, amount * (len(trades) + 1) - shares * nav)
            gross = float(decimal(min(target * len(due[key]), max_buy, cash)))
            fee = float(
                decimal(
                    gross * fee_rate
                    if fee_basis == "gross"
                    else gross - gross / (1 + fee_rate)
                )
            )
            sh = float(decimal(max(0, gross - fee) / nav, "0.0001"))
            actual = sh * nav + fee
            if actual > cash + 0.011:
                raise ValueError("现金不足")
            shares += sh
            cash -= actual
            spent += actual
            fees += fee
            if sh:
                lots.append({"date": key, "shares": sh})
            trades.append(
                {
                    "date": key,
                    "nav": nav,
                    "amount": round(actual, 2),
                    "fee": fee,
                    "shares": sh,
                    "cum_shares": shares,
                    "mult": mult
                    if strategy in ("valuation", "price_position")
                    else 1.0,
                    "scheduled_dates": [x["scheduled_date"] for x in due[key]],
                }
            )
        equity = shares * nav + cash + sum(x["amount"] for x in receivables)
        if previous > 0:
            neutral *= max(0, (equity - inflow) / previous)
        elif inflow > 0:
            neutral *= equity / inflow
        previous = equity
        curve.append(
            {
                "date": key,
                "value": round(equity, 2),
                "market_value": round(shares * nav, 2),
                "cash": round(cash, 2),
                "receivable": round(sum(x["amount"] for x in receivables), 2),
                "contributed": round(contributed, 2),
                "nav": neutral,
            }
        )
    from fund_dca import xirr
    from fund.fund_fees import redemption_charge

    exit_details = (
        redemption_charge(lots, nav, str(f.index[-1].date()), redemption_fee_schedule)
        if redemption_fee_schedule
        else None
    )
    exit_fee = (
        exit_details["fee"]
        if exit_details
        else float(decimal(shares * nav * redemption_fee))
    )
    final = previous - exit_fee
    cashflows.append((f.index[-1], final))
    navs = np.array([1.0] + [x["nav"] for x in curve])
    drawdown = float(np.max(1 - navs / np.maximum.accumulate(navs)))
    # Same cash arrivals baseline, subscriptions on every scheduled execution, no timing tilt.
    baseline = None
    if strategy != "normal":
        baseline = simulate(
            frame,
            amount,
            period,
            day,
            fee_rate,
            start,
            end,
            "normal",
            anchor_date=anchor_date,
            total_budget=budget,
            max_period_amount=max_buy,
            fee_basis=fee_basis,
            dividend_mode=dividend_mode,
            redemption_fee=redemption_fee,
            redemption_fee_schedule=redemption_fee_schedule,
            open_dates=open_dates,
        )
        baseline = {
            k: baseline.get(k) for k in ("final_value", "profit_pct", "annualized_irr")
        }
    from fund.fund_contracts import engine_hash

    config = {
        "engine_hash": engine_hash(),
        "code": frame.attrs.get("code"),
        "version": VERSION,
        "amount": amount,
        "period": period,
        "day": day,
        "strategy": strategy,
        "anchor_date": anchor_date,
        "budget": budget,
        "max_period_amount": max_buy,
        "fee_rate": fee_rate,
        "fee_basis": fee_basis,
        "dividend_mode": dividend_mode,
        "redemption_fee": redemption_fee,
        "start": str(f.index[0].date()),
        "end": str(f.index[-1].date()),
        "redemption_fee_schedule": redemption_fee_schedule,
        "open_dates": [str(x)[:10] for x in open_dates]
        if open_dates is not None
        else None,
        "valuation_snapshot": fingerprint(valuation_series.to_dict("records"))
        if valuations is not None
        else None,
    }
    # Reuse the same event/fee engine for the separately labelled initial-capital scenario.
    lump_result = None
    if not _lump_once:
        lump_result = simulate(
            frame,
            contributed,
            period,
            day,
            fee_rate,
            start,
            end,
            "normal",
            anchor_date=anchor_date,
            total_budget=contributed,
            max_period_amount=contributed,
            fee_basis=fee_basis,
            dividend_mode=dividend_mode,
            redemption_fee=redemption_fee,
            redemption_fee_schedule=redemption_fee_schedule,
            open_dates=open_dates,
            _lump_once=True,
        )
    return {
        "status": "ready",
        "schema_version": VERSION,
        **config,
        "quality": quality,
        "snapshot_id": quality["snapshot_id"],
        "experiment_id": fingerprint(
            {"config": config, "snapshot": quality["snapshot_id"], "actions": events}
        ),
        "assumptions": {
            "calendar_basis": "user_supplied_product_calendar"
            if open_dates is not None
            else "净值观察日作为可申购日的研究假设",
            "execution_feasibility": "unverified",
            "pit_status": "用户提供可知时间，未自动认定历史独立验证"
            if strategy == "valuation"
            else "固定日程或前一观察日信号",
            "execution": "scheduled_end_of_day_nav",
            "operations_fees": "included_in_nav",
            "unknown_dividend_payment": "receivable_not_spendable",
            "actions": frame.attrs.get("actions_status", "caller_supplied_no_events"),
        },
        "total_invested": round(contributed, 2),
        "total_spent": round(spent, 2),
        "total_fee": round(fees + exit_fee, 2),
        "redemption_details": exit_details,
        "highest_investment": max(t["amount"] for t in trades),
        "idle_cash": round(cash, 2),
        "cum_shares": shares,
        "cost_nav": spent / shares if shares else None,
        "end_nav": nav,
        "n_invests": sum(t["shares"] > 0 for t in trades),
        "final_value": round(final, 2),
        "profit": round(final - contributed, 2),
        "profit_pct": final / contributed - 1 if contributed else None,
        "annualized_irr": xirr(cashflows),
        "max_drawdown": drawdown,
        "time_weighted_return": neutral - 1,
        "same_cash_baseline": baseline,
        "lump_sum": {
            "assumption": "全部资金期初已可用，使用相同分红及费用规则",
            "status": lump_result.get("status"),
            "error": lump_result.get("error"),
            "final_value": lump_result.get("final_value"),
            "profit_pct": lump_result.get("profit_pct"),
        }
        if lump_result
        else None,
        "dca_beats_lump": None,
        "trades": trades,
        "equity_curve": curve,
    }
