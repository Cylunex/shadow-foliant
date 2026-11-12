"""Human-readable projection of the authoritative intraday snapshot."""
from notify.stock_report import report, text, price
from application.holding_review import ordered_holdings, risk_label


def render(snapshot, label, *, exit_review=None):
    from jobs.intraday_decision_monitor import _event_label, _holding_guard_reason
    from application.market_risk_notice import index_lines
    quality = snapshot.get("data_quality") or {}
    as_of = quality.get("quote_as_of") or snapshot.get("generated_at") or ""
    holdings = snapshot.get("holdings") or []
    counts = exit_review["counts"] if exit_review else (snapshot.get("position_counts") or {})
    market = index_lines(snapshot.get("market_risk") or {})
    if counts:
        market.append(f"持仓：个股{counts.get('equity_count', 0)}只，ETF {counts.get('etf_count', 0)}只，"
                      f"其他基金{counts.get('other_fund_count', 0)}只。"
                      + (f"个股较15只参考上限多{counts['excess']}只。" if counts.get('excess') else ""))
        if exit_review:
            market.append(f"清仓目标：个股 {counts['equity_count']}/15，超 {counts['excess']} 只；按风险优先级逐项复核。")
    review_pool = holdings
    if exit_review:
        equity_symbols = set(exit_review["counts"]["equity_symbols"])
        review_pool = [r for r in holdings if r.get("symbol") in equity_symbols]
        market.append(next((line for line in exit_review["text"].splitlines()
                            if line.startswith("判断变化｜")), ""))
    shown = ordered_holdings(review_pool)[:3]
    if exit_review:
        other_risks = [r for r in holdings if r.get("symbol") not in equity_symbols
                       and (r.get("action") in {"sell", "reduce"}
                            or r.get("requested_action") in {"sell", "reduce"})]
        shown += ordered_holdings(other_risks)[:1]
    def current_price(row):
        if not row.get("price_actionable") or row.get("action") == "data_insufficient":
            return "暂不给价"
        value = price(row.get("price"))
        return "¥" + value if value != "待核验" else "暂不给价"
    actions = []
    labels = {"sell": "退出复核", "reduce": "部分减仓复核", "add": "加仓观察",
              "buy": "买入观察", "hold": "观望", "data_insufficient": "暂缓判断"}
    for row in shown:
        action = row.get("action")
        reason = text(str(row.get("reason") or "条件尚未明确").replace("trade_plan ", "计划"), 45)
        requested = row.get("requested_action")
        if requested and requested != action:
            original = text(str(row.get('requested_reason') or labels.get(requested, '风险提示')).replace("trade_plan ", "计划"), 40)
            reason = f"原信号：{original}；处理限制：{text(_holding_guard_reason(row), 45)}；风险未解除"
        cost = price(row.get("cost_price"))
        cost_note = f"，成本 {cost}" if cost != "待核验" else "，成本待核验"
        scope = "其他持仓风险 · " if exit_review and row.get("symbol") not in equity_symbols else ""
        actions.append(f"• {scope}{risk_label(row)}：{_event_label({'symbol': row['symbol'], 'item': row}, snapshot)}"
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
        reason = text(str(row.get("reason") or "已入选项目候选，买点待核验").replace("trade_plan ", "计划"), 90)
        candidates.append(f"• {_event_label({'symbol': code, 'item': row}, snapshot)}："
                          f"现价{current_price(row)}。{reason}。")
        if len(candidates) == 3:
            break
    notes = ["以上为分析参考，未发生交易；候选排名不代表买入时机。"]
    if any(row.get("requested_action") in {"sell", "reduce"}
           and row.get("requested_action") != row.get("action") for row in holdings):
        notes.append("风险排序保留原始触发；组合限额仅限制本轮处理，不表示风险减轻。")
    if exit_review:
        notes.append("仅计个股名额；部分减仓不减少只数，实际清仓成交入账后更新。操作前核对可卖量。")
    elif label == "14:30":
        notes.append("按本轮同一优先级重排；尾盘行情与复盘结论可能改变早盘顺序。")
    if quality.get("status") != "success":
        notes.append("数据说明：部分行情或计划待核验，相关证券暂不给价、暂缓动作判断。")
    period = "早盘清仓" if exit_review else {"10:05": "早盘", "11:20": "午间", "14:30": "尾盘"}.get(label, "盘中")
    return report(period, as_of, [
        ("1）盘面概况", market),
        ("2）优先退出复核（成本对照）" if exit_review else "2）持仓动作（成本对照）", actions),
        ("3）新选股观察（非持仓、无688）", candidates or ["本轮暂无符合条件的新候选。"]),
    ], notes)
