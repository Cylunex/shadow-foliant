"""Human-readable projection of the authoritative intraday snapshot."""
from notify.stock_report import report, text, price


def render(snapshot, label):
    from jobs.intraday_decision_monitor import ACTION_RANK, _event_label, _holding_guard_reason
    from application.market_risk_notice import index_lines
    quality = snapshot.get("data_quality") or {}
    as_of = quality.get("quote_as_of") or snapshot.get("generated_at") or ""
    holdings = snapshot.get("holdings") or []
    counts = snapshot.get("position_counts") or {}
    market = index_lines(snapshot.get("market_risk") or {})
    if counts:
        market.append(f"持仓：个股{counts.get('equity_count', 0)}只，ETF {counts.get('etf_count', 0)}只，"
                      f"其他基金{counts.get('other_fund_count', 0)}只。"
                      + (f"个股较15只参考上限多{counts['excess']}只。" if counts.get('excess') else ""))
    shown = sorted(holdings, key=lambda row: (
        -max(ACTION_RANK.get(str(row.get("action")), -1),
             ACTION_RANK.get(str(row.get("requested_action")), -1)),
        -ACTION_RANK.get(str(row.get("action")), -1),
    ))[:3]
    def current_price(row):
        if not row.get("price_actionable") or row.get("action") == "data_insufficient":
            return "暂不给价"
        value = price(row.get("price"))
        return "¥" + value if value != "待核验" else "暂不给价"
    actions = []
    labels = {"sell": "卖出观察", "reduce": "减仓参考", "add": "加仓观察",
              "buy": "买入观察", "hold": "观望", "data_insufficient": "暂缓判断"}
    for row in shown:
        action = row.get("action")
        reason = text(str(row.get("reason") or "条件尚未明确").replace("trade_plan ", "计划"), 45)
        requested = row.get("requested_action")
        if requested and requested != action:
            original = text(str(row.get('requested_reason') or labels.get(requested, '风险提示')).replace("trade_plan ", "计划"), 40)
            reason = f"原信号：{original}；本轮{labels.get(action, '观望')}：{text(_holding_guard_reason(row), 45)}；风险未解除"
        cost = price(row.get("cost_price"))
        cost_note = f"，成本 {cost}" if cost != "待核验" else "，成本待核验"
        actions.append(f"• {labels.get(action, '观望')}：{_event_label({'symbol': row['symbol'], 'item': row}, snapshot)}"
                       f" — 现价{current_price(row)}{cost_note}。\n  {reason}。")
    if not actions:
        actions = ["本轮暂无已核验的持仓动作。"]
    elif len(holdings) > len(shown):
        actions.append(f"以上为{len(holdings)}只持仓中的{len(shown)}只重点，其余持仓未在本条展开。")
    held = {str(row.get("symbol")) for row in holdings}
    candidates, seen = [], set()
    for row in (snapshot.get("formal_top5") or []) + (snapshot.get("formal_top15_watch") or []):
        code = str(row.get("symbol") or "")
        if not code or code in held or code in seen or code.startswith("688") or "holding" in (row.get("sources") or []):
            continue
        seen.add(code)
        reason = text(str(row.get("reason") or "已入选项目候选，买点待核验").replace("trade_plan ", "计划"), 55)
        candidates.append(f"• {_event_label({'symbol': code, 'item': row}, snapshot)}："
                          f"现价{current_price(row)}。{reason}。")
        if len(candidates) == 3:
            break
    notes = ["以上为分析参考，未发生交易；候选排名不代表买入时机。"]
    if quality.get("status") != "success":
        notes.append("数据说明：部分行情或计划待核验，相关证券暂不给价、暂缓动作判断。")
    period = {"10:05": "早盘", "11:20": "午间", "14:30": "尾盘"}.get(label, "盘中")
    return report(period, as_of, [
        ("1）盘面概况", market),
        ("2）持仓动作（成本对照）", actions),
        ("3）新选股观察（非持仓、无688）", candidates or ["本轮暂无符合条件的新候选。"]),
    ], notes)
