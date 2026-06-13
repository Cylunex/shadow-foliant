import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""加仓信号"双推送"审核

核心场景：
  你的策略 = 跌幅 N% 加仓（金字塔抄底）
  风险     = 越加越跌、价值陷阱、单股集中
  设计     = 系统**不拦截**你的操作，但把"该加仓"和"该止损"两种情况
            用截然不同的推送文案标清楚 —— 让你决策时心里有数

接口：
  evaluate_one(symbol)               单股审核
  evaluate_all_triggered()           扫所有持仓，返回触发跌幅的股 + 各自审核结果
  format_alert(verdict_dict)         格式化为推送文本

依赖参数（来自 user_strategy_config）：
  - drop_trigger_pct_today    当日跌幅触发
  - drop_trigger_pct_holding  持仓盈亏跌幅触发
  - max_position_pct          仓位上限
  - max_add_times             加仓次数上限
  - fundamental_min_score     基本面打分门槛
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Any

import user_strategy_config as cfg


def _portfolio_db():
    """统一 PG/SQLite 入口"""
    use_pg = os.getenv('USE_POSTGRES', '').lower() in ('1', 'true', 'yes', 'on')
    if use_pg:
        try:
            from portfolio_db_pg import portfolio_db
            return portfolio_db
        except Exception:
            pass
    from portfolio_db import portfolio_db
    return portfolio_db


def _get_current_price(symbol: str) -> Optional[float]:
    """拿最新价（优先 a-stock HTTP，回退 stock_data）"""
    try:
        import datahub
        q = datahub.quote(symbol)
        if isinstance(q, dict) and q.get('price'):
            return float(q['price'])
    except Exception:
        pass
    try:
        import datahub
        df = datahub.kline(symbol, '1mo')
        if df is not None and len(df) > 0:
            close_col = 'Close' if 'Close' in df.columns else 'close'
            return float(df[close_col].iloc[-1])
    except Exception:
        pass
    return None


def _get_add_times(symbol: str) -> int:
    """从 portfolio_changes 拉过去 365 天加仓次数"""
    try:
        from db_compat import connect, USE_POSTGRES
        snap_db = _bootstrap.db_path('jobs_snapshots.db')
        conn = connect(snap_db)
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute('''
                SELECT COUNT(*) FROM portfolio_changes
                WHERE code = ? AND delta_qty > 0
                  AND changed_at >= NOW() - INTERVAL '365 days'
            ''', (symbol,))
        else:
            cur.execute('''
                SELECT COUNT(*) FROM portfolio_changes
                WHERE code = ? AND delta_qty > 0
                  AND changed_at >= datetime('now', '-365 days')
            ''', (symbol,))
        n = cur.fetchone()[0] or 0
        conn.close()
        return int(n)
    except Exception:
        return 0


def _get_fundamental_score(symbol: str) -> Optional[Dict[str, Any]]:
    """复用 fundamental_scoring 模块"""
    try:
        from fundamental_scoring import score_one
        return score_one(symbol)
    except Exception as e:
        return None


def _get_total_capital() -> Optional[float]:
    """估算总资金 = 持仓市值 + 未投入现金（这里只算持仓市值，作为下界）

    实际生产中你可以在 user_strategy_config 加 'total_capital' 项，
    或者在 portfolio_db 加 'cash' 字段。当前简化版按持仓总市值估算。
    """
    try:
        pdb = _portfolio_db()
        stocks = pdb.get_all_stocks() or []
        total = 0.0
        for s in stocks:
            price = _get_current_price(s.get('code', '')) or s.get('cost_price', 0) or 0
            qty = s.get('quantity', 0) or 0
            total += float(price) * float(qty)
        # 默认按持仓市值 * 1.4 估算（假设 70% 仓位），可在 user_strategy_config 中调
        multiplier = cfg.get('total_capital_multiplier', 1.4)
        return total * multiplier if total > 0 else None
    except Exception:
        return None


def evaluate_one(symbol: str, stock_info: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """审核单股加仓资格

    Returns:
        None: 未触发跌幅条件
        dict: {
          symbol, name,
          verdict: 'approve' | 'reject',
          reason_codes: [...],
          fundamental_grade, fundamental_score,
          current_position_pct, add_times,
          current_price, cost_price,
          today_change_pct, holding_pnl_pct,
          recommendation: str (推送用文本)
        }
    """
    pdb = _portfolio_db()
    if stock_info is None:
        stock_info = pdb.get_stock_by_code(symbol)
    if not stock_info:
        return None

    cost_price = float(stock_info.get('cost_price', 0) or 0)
    qty = float(stock_info.get('quantity', 0) or 0)
    name = stock_info.get('name', '')
    if cost_price <= 0 or qty <= 0:
        return None

    current_price = _get_current_price(symbol)
    if current_price is None or current_price <= 0:
        return None

    holding_pnl_pct = (current_price - cost_price) / cost_price * 100

    today_change_pct = 0.0
    try:
        from stock_data import StockDataFetcher
        df = StockDataFetcher().get_stock_data(symbol, '1mo')
        if df is not None and len(df) >= 2:
            close_col = 'Close' if 'Close' in df.columns else 'close'
            today = float(df[close_col].iloc[-1])
            yest = float(df[close_col].iloc[-2])
            today_change_pct = (today - yest) / yest * 100
    except Exception:
        pass

    drop_today = abs(cfg.get('drop_trigger_pct_today', 2.0))
    drop_holding = abs(cfg.get('drop_trigger_pct_holding', 5.0))

    triggered = (today_change_pct <= -drop_today) or (holding_pnl_pct <= -drop_holding)
    if not triggered:
        return None

    add_times = _get_add_times(symbol)
    position_value = current_price * qty
    total_cap = _get_total_capital() or position_value * 10
    position_pct = position_value / total_cap * 100 if total_cap > 0 else 0

    fund = _get_fundamental_score(symbol)
    fund_score = (fund or {}).get('score') if fund else None
    fund_grade = (fund or {}).get('grade', 'N/A')

    reasons: List[str] = []

    max_pos_pct = cfg.get('max_position_pct', 10.0)
    if position_pct >= max_pos_pct:
        reasons.append(f'仓位已达 {position_pct:.1f}% (上限 {max_pos_pct}%)')

    max_add = cfg.get('max_add_times', 5)
    if add_times >= max_add:
        reasons.append(f'已加仓 {add_times} 次 (上限 {max_add})')

    fund_min = cfg.get('fundamental_min_score', 50.0)
    if fund_score is not None and fund_score < fund_min:
        reasons.append(f'基本面打分 {fund_score} 低于门槛 {fund_min} ({fund_grade})')
    elif fund and fund.get('low_coverage'):
        reasons.append(f'基本面数据覆盖不足 — 谨慎')

    verdict = 'reject' if reasons else 'approve'
    return {
        'symbol': symbol,
        'name': name,
        'verdict': verdict,
        'reason_codes': reasons,
        'fundamental_grade': fund_grade,
        'fundamental_score': fund_score,
        'current_position_pct': round(position_pct, 2),
        'add_times': add_times,
        'current_price': current_price,
        'cost_price': cost_price,
        'today_change_pct': round(today_change_pct, 2),
        'holding_pnl_pct': round(holding_pnl_pct, 2),
        'recommendation': _format_recommendation(symbol, name, verdict, reasons,
                                                  today_change_pct, holding_pnl_pct,
                                                  position_pct, fund_grade, add_times),
    }


def _format_recommendation(symbol, name, verdict, reasons,
                           today_chg, holding_pnl, pos_pct, fund_grade, add_times) -> str:
    if verdict == 'approve':
        return (f'✅ [加仓建议] {symbol} {name}\n'
                f'  当日 {today_chg:+.2f}% / 持仓盈亏 {holding_pnl:+.2f}%\n'
                f'  基本面 {fund_grade} | 当前仓位 {pos_pct:.1f}% | 已加 {add_times} 次\n'
                f'  👉 触发加仓条件，质地审核通过，可考虑加 1-2 手')
    return (f'⚠️ [加仓警告] {symbol} {name}\n'
            f'  当日 {today_chg:+.2f}% / 持仓盈亏 {holding_pnl:+.2f}%\n'
            f'  基本面 {fund_grade} | 当前仓位 {pos_pct:.1f}% | 已加 {add_times} 次\n'
            f'  ⛔ 触发加仓条件但被多项硬约束拒绝：\n'
            + '\n'.join(f'     - {r}' for r in reasons) + '\n'
            f'  👉 反向建议：考虑止损 30-50% 而非加仓')


def evaluate_all_triggered() -> List[Dict[str, Any]]:
    """扫所有持仓，返回触发跌幅的股 + 各自审核结果"""
    pdb = _portfolio_db()
    stocks = pdb.get_all_stocks() or []
    out = []
    for s in stocks:
        code = s.get('code')
        if not code:
            continue
        try:
            r = evaluate_one(code, stock_info=s)
            if r is not None:
                out.append(r)
        except Exception as e:
            print(f'[position_guardian] {code} 审核失败: {e}')
    return out


def format_alert(items: List[Dict[str, Any]]) -> str:
    """组合多条审核结果为一份完整推送文本"""
    if not items:
        return '当前无加仓触发的持仓股'
    approve = [x for x in items if x['verdict'] == 'approve']
    reject = [x for x in items if x['verdict'] == 'reject']
    lines = [f'📊 持仓加仓信号扫描 — {datetime.now().strftime("%Y-%m-%d %H:%M")}']
    if approve:
        lines.append(f'\n━━━ ✅ 建议加仓 ({len(approve)} 只) ━━━')
        for x in approve:
            lines.append(x['recommendation'])
    if reject:
        lines.append(f'\n━━━ ⚠️ 加仓警告 — 该止损不该加 ({len(reject)} 只) ━━━')
        for x in reject:
            lines.append(x['recommendation'])
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== position_guardian 自检 ===')
    items = evaluate_all_triggered()
    print(f'触发跌幅的持仓: {len(items)}')
    print()
    print(format_alert(items))
