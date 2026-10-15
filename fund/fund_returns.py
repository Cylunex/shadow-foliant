"""Distribution/split aware return series, shared by research and simulation."""

from __future__ import annotations
import pandas as pd


def return_frame(frame):
    out = frame.copy().sort_values("date")
    out["date"] = pd.to_datetime(out["date"])
    if out["date"].duplicated().any():
        raise ValueError("重复净值日期")
    s = pd.to_numeric(out["unit_nav"], errors="coerce")
    if s.isna().any() or (s <= 0).any():
        raise ValueError("无效净值")
    events_known = (
        frame.attrs.get("actions_status") == "ready"
        or "cash_dividend" in out
        or "split_ratio" in out
    )
    div = pd.to_numeric(
        out.get("cash_dividend", pd.Series(0.0, index=out.index)), errors="raise"
    ).fillna(0)
    split = pd.to_numeric(
        out.get("split_ratio", pd.Series(1.0, index=out.index)), errors="raise"
    ).fillna(1)
    if (split <= 0).any() or (div < 0).any():
        raise ValueError("无效权益事件")
    if "income_per_10000" in out:
        returns = pd.to_numeric(out["income_per_10000"], errors="raise") / 10000
        div = returns
        basis = "money_income"
    elif events_known:
        # Cash dividend is per pre-event share; split factor is new / old shares.
        returns = (s * split + div) / s.shift(1) - 1
        basis = "events_total_return"
    elif (
        "daily_return" in out
        and pd.to_numeric(out["daily_return"], errors="coerce").iloc[1:].notna().all()
    ):
        returns = pd.to_numeric(out["daily_return"], errors="coerce") / 100
        basis = "disclosed_total_return"
    else:
        returns = s.pct_change(fill_method=None)
        basis = "unadjusted_unit_nav"
    out["cash_dividend"], out["split_ratio"] = div, split
    out["return"] = returns.fillna(0)
    out["total_return_nav"] = (1 + out["return"]).cumprod()
    out.attrs.update(frame.attrs)
    out.attrs["return_basis"] = basis
    return out


def portfolio_performance(marks):
    """TTWROR from daily account marks: inflow at start, outflow at end (PP Eq.3)."""
    from fund.fund_contracts import number, day
    from fund_dca import xirr

    marks = sorted(marks, key=lambda r: day(r["date"]))
    if len(marks) < 2:
        return {"status": "partial", "reason": "至少需要两个完整账户估值点"}
    if len({day(r["date"]) for r in marks}) != len(marks):
        raise ValueError("重复账户估值日期")
    equity = [number(r["equity"]) for r in marks]
    if min(equity) < 0:
        raise ValueError("账户估值不能为负")
    unit = peak = 1.0
    drawdown = 0.0
    curve = []
    flows = [(pd.Timestamp(marks[0]["date"]), -equity[0])]
    for i, r in enumerate(marks):
        if i:
            cin = number(r.get("external_in") or 0)
            cout = number(r.get("external_out") or 0)
            if cin < 0 or cout < 0:
                raise ValueError("转入转出须为非负数")
            base = equity[i - 1] + cin
            if base <= 0:
                return {"status": "partial", "reason": "资金基数不可计算"}
            unit *= (equity[i] + cout) / base
            peak = max(peak, unit)
            drawdown = max(drawdown, 1 - unit / peak)
            flows.append((pd.Timestamp(r["date"]), cout - cin))
        curve.append({"date": day(r["date"]), "equity": equity[i], "nav": unit})
    flows.append((pd.Timestamp(marks[-1]["date"]), equity[-1]))
    return {
        "status": "ready",
        "time_weighted_return": unit - 1,
        "max_drawdown": drawdown,
        "annualized_irr": xirr(flows),
        "curve": curve,
        "basis": "完整账户估值，外部资金日初流入、日末流出；买卖和分红须已包含在账户资产中",
    }
