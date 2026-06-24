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
        # ⚡ 用成本价(库内,零网络)估算总市值——原来逐只 _get_current_price,
        # 而本函数在 evaluate_one 里**每只触发股都调一次** → N×全持仓 次实时报价(N² 爆炸,扫描卡死)。
        # 总资金只是仓位占比的分母,成本口径足够;要实时口径可在 user_strategy_config 配 total_capital。
        cfg_cap = cfg.get('total_capital', 0)
        if cfg_cap and float(cfg_cap) > 0:
            return float(cfg_cap)
        total = 0.0
        for s in stocks:
            price = float(s.get('cost_price', 0) or 0)
            qty = float(s.get('quantity', 0) or 0)
            total += price * qty
        multiplier = cfg.get('total_capital_multiplier', 1.4)  # 假设 ~70% 仓位
        return total * multiplier if total > 0 else None
    except Exception:
        return None


def evaluate_one(symbol: str, stock_info: Optional[Dict] = None,
                 with_fundamental: bool = True) -> Optional[Dict[str, Any]]:
    """审核单股加仓资格(with_fundamental=False 跳过慢的基本面评分,仅按 仓位/加仓次数 硬约束)

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
        import datahub
        df = datahub.kline(symbol, '1mo')   # 走 datahub:磁盘缓存+多源兜底,避 StockDataFetcher 冷拉卡死
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

    fund = _get_fundamental_score(symbol) if with_fundamental else None
    fund_score = (fund or {}).get('score') if fund else None
    fund_grade = (fund or {}).get('grade', 'N/A')

    reasons: List[str] = []

    max_pos_pct = cfg.get('max_position_pct', 10.0)
    if position_pct >= max_pos_pct:
        reasons.append(f'仓位已经太重({position_pct:.1f}%,超过上限{max_pos_pct}%)')

    max_add = cfg.get('max_add_times', 5)
    if add_times >= max_add:
        reasons.append(f'已经加了{add_times}次(上限{max_add}次),别再摊')

    fund_min = cfg.get('fundamental_min_score', 50.0)
    if fund_score is not None and fund_score < fund_min:
        reasons.append(f'基本面太弱({fund_score}分,低于{fund_min})')
    elif fund and fund.get('low_coverage'):
        reasons.append('基本面查不到足够数据,质地看不清')

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
    head = f'{symbol} {name}'.strip()
    stat = (f'  今日 {today_chg:+.2f}%，持仓 {holding_pnl:+.2f}%，'
            f'仓位 {pos_pct:.1f}%，已加 {add_times} 次')
    if verdict == 'approve':
        return (f'✅ 可以加仓 ｜ {head}\n'
                f'{stat}\n'
                f'  跌到你的抄底点、质地也过关 → 可加 1-2 手')
    why = '；'.join(reasons) if reasons else '触发多项风控'
    # 仅"查不到基本面数据"——这是看不清、不是已知很差,别喊止损,先观望就好
    only_coverage = bool(reasons) and all('查不到' in r for r in reasons)
    if only_coverage:
        return (f'🔻 先别加 ｜ {head}\n'
                f'{stat}\n'
                f'  为什么:{why}\n'
                f'  → 看不清质地,先观望,别越跌越补')
    return (f'🔻 别加仓,该减就减 ｜ {head}\n'
            f'{stat}\n'
            f'  为什么:{why}\n'
            f'  → 越跌越买容易越套越深,建议减仓/止损,别补仓')


def evaluate_all_triggered(max_workers: int = 8, limit: int = 0,
                           with_fundamental: bool = True) -> List[Dict[str, Any]]:
    """扫所有持仓，返回触发跌幅的股 + 各自审核结果。每只独立(实时价+K线+基本面)→ 并发跑。
    limit>0 时只扫市值最大前 N 只(冷门小仓外部源易卡);with_fundamental=False 跳过慢的基本面评分。"""
    from concurrent.futures import ThreadPoolExecutor
    pdb = _portfolio_db()
    stocks = [s for s in (pdb.get_all_stocks() or []) if s.get('code')]
    if limit and limit > 0 and len(stocks) > limit:
        stocks.sort(key=lambda s: float(s.get('cost_price') or 0) * float(s.get('quantity') or 0), reverse=True)
        stocks = stocks[:limit]
    if not stocks:
        return []

    def _one(s):
        try:
            return evaluate_one(s['code'], stock_info=s, with_fundamental=with_fundamental)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=min(max_workers, len(stocks))) as ex:
        return [r for r in ex.map(_one, stocks) if r is not None]


def format_alert(items: List[Dict[str, Any]]) -> str:
    """组合多条审核结果为一份完整推送文本"""
    if not items:
        return '当前无加仓触发的持仓股'
    approve = [x for x in items if x['verdict'] == 'approve']
    reject = [x for x in items if x['verdict'] == 'reject']
    lines = [f'📊 越跌越买·提示 — {datetime.now().strftime("%Y-%m-%d %H:%M")}',
             '_持仓里今天跌到你抄底点的票,哪些能补、哪些该减_']
    if approve:
        lines.append(f'\n━━━ ✅ 这些可以加 ({len(approve)} 只) ━━━')
        for x in approve:
            lines.append(x['recommendation'])
    if reject:
        lines.append(f'\n━━━ 🔻 这些别加,该减就减 ({len(reject)} 只) ━━━')
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
