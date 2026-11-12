"""Shared display priority; never changes the underlying trading decision."""

ACTION_RANK = {"data_insufficient": -1, "hold": 0, "add": 1, "reduce": 2, "sell": 3}


def ordered_holdings(rows):
    def priority(row):
        action = str(row.get("action") or "hold")
        requested = str(row.get("requested_action") or action)
        return (-max(ACTION_RANK.get(action, -1), ACTION_RANK.get(requested, -1)),
                -ACTION_RANK.get(action, -1),
                -float(row.get("exit_score") or 0),
                float(row.get("holding_pnl_pct") or 0),
                str(row.get("symbol") or row.get("code") or ""))
    return sorted(rows, key=priority)


def risk_label(row):
    action = row.get("action")
    requested = row.get("requested_action")
    if requested in {"sell", "reduce"} and action not in {"sell", "reduce"}:
        return "退出风险待处理" if requested == "sell" else "减仓风险待处理"
    return {"sell": "退出复核", "reduce": "部分减仓复核", "add": "加仓观察",
            "buy": "买入观察", "hold": "观望", "data_insufficient": "暂缓判断"}.get(action, "观望")
