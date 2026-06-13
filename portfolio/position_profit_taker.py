"""持仓减仓信号扫描（方案 A：温和阶梯 + MA 趋势保护）

3 阶梯减仓（按持仓均价计算的涨幅）：
  涨 ≥ profit_take_1_pct (默认 30%)  →  减 30%   (回本+部分锁定)
  涨 ≥ profit_take_2_pct (默认 60%)  →  减 30%   (继续锁定)
  涨 ≥ profit_take_3_pct (默认 100%) →  减 30%   (剩 10% 博梦想)

MA 趋势保护（开关 enable_ma_stop_loss，无视盈利状态）：
  跌破 MA20 (ma_stop_short) →  减 50%   (保护盈利)
  跌破 MA60 (ma_stop_long)  →  清仓     (趋势已坏)

避免重复推送：
  本扫描设计为每日 1 次（盘后 15:55）。系统只发提醒，不自动下单。
  你减仓后下次扫描会基于新持仓数量+均价重新算，自然不会"重复推"同一阶段。

接口：
  evaluate_one(symbol, stock_info=None)  单股扫描
  evaluate_all()                          扫所有持仓，返回触发的列表
  format_alert(items)                     格式化为推送文本
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Any

import user_strategy_config as cfg
from position_guardian import _portfolio_db, _get_current_price


def _check_ma_break(symbol: str, ma_short: int, ma_long: int) -> Dict[str, Any]:
    """返回 {'last', 'ma_short', 'ma_long', 'broke_short', 'broke_long'}"""
    result = {'last': None, 'ma_short': None, 'ma_long': None,
              'broke_short': False, 'broke_long': False}
    try:
        from stock_data import StockDataFetcher
        df = StockDataFetcher().get_stock_data(symbol, '1y')
        if df is None or len(df) < max(ma_short, ma_long):
            return result
        close_col = 'Close' if 'Close' in df.columns else 'close'
        closes = df[close_col].astype('float64')
        last = float(closes.iloc[-1])
        ma_s = float(closes.tail(ma_short).mean())
        ma_l = float(closes.tail(ma_long).mean())
        result['last'] = last
        result['ma_short'] = ma_s
        result['ma_long'] = ma_l
        result['broke_short'] = last < ma_s
        result['broke_long'] = last < ma_l
    except Exception:
        pass
    return result


def evaluate_one(symbol: str, stock_info: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
    """扫单股，找出该推哪条减仓信号。返回 None 表示无信号"""
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

    cur = _get_current_price(symbol)
    if cur is None or cur <= 0:
        return None

    profit_pct = (cur - cost_price) / cost_price * 100

    p1 = cfg.get('profit_take_1_pct', 30.0)
    p2 = cfg.get('profit_take_2_pct', 60.0)
    p3 = cfg.get('profit_take_3_pct', 100.0)

    # 优先 MA 趋势保护（最严：跌破 MA60 直接清仓）
    enable_ma = cfg.get('enable_ma_stop_loss', True)
    ma_short_n = int(cfg.get('ma_stop_short', 20))
    ma_long_n = int(cfg.get('ma_stop_long', 60))
    ma = _check_ma_break(symbol, ma_short_n, ma_long_n) if enable_ma else {}

    actions: List[Dict[str, Any]] = []

    # MA 触发
    if enable_ma and ma.get('broke_long'):
        actions.append({
            'severity': 'critical',
            'action_pct': 100,
            'reason': f'⛔ 跌破 MA{ma_long_n} (现价 {cur:.2f} < MA{ma_long_n} {ma["ma_long"]:.2f}) — 趋势已坏',
            'recommendation': '清仓剩余',
        })
    elif enable_ma and ma.get('broke_short'):
        actions.append({
            'severity': 'warning',
            'action_pct': 50,
            'reason': f'⚠️ 跌破 MA{ma_short_n} (现价 {cur:.2f} < MA{ma_short_n} {ma["ma_short"]:.2f}) — 短期破位',
            'recommendation': '减 50% 保护盈利',
        })

    # 阶梯减仓（按盈利状态）
    if profit_pct >= p3:
        actions.append({
            'severity': 'info',
            'action_pct': 30,
            'reason': f'✅ 涨 {profit_pct:.1f}% (≥ {p3}%) — 阶梯 3',
            'recommendation': '建议减 30%（剩 10% 长持博梦想）',
        })
    elif profit_pct >= p2:
        actions.append({
            'severity': 'info',
            'action_pct': 30,
            'reason': f'✅ 涨 {profit_pct:.1f}% (≥ {p2}%) — 阶梯 2',
            'recommendation': '建议减 30%（继续锁定盈利）',
        })
    elif profit_pct >= p1:
        actions.append({
            'severity': 'info',
            'action_pct': 30,
            'reason': f'✅ 涨 {profit_pct:.1f}% (≥ {p1}%) — 阶梯 1',
            'recommendation': '建议减 30%（回本并部分锁定）',
        })

    # 情绪过热预警(借 strategy_signals):乖离 MA20 过大 + 放量 → 提示锁利,叠加在阶梯之上
    try:
        from stock_data import StockDataFetcher
        from strategy_signals import emotion_top_warning
        _df = StockDataFetcher().get_stock_data(symbol, '6mo')
        if not isinstance(_df, dict):
            et = emotion_top_warning(_df)
            if et.get('signal'):
                actions.append({
                    'severity': 'warning',
                    'action_pct': 30,
                    'reason': f'🔥 情绪过热:{et.get("reason", "")}',
                    'recommendation': '情绪过热,考虑减仓锁利(勿追高)',
                })
    except Exception:
        pass

    if not actions:
        return None

    return {
        'symbol': symbol,
        'name': name,
        'cost_price': cost_price,
        'current_price': cur,
        'quantity': qty,
        'profit_pct': round(profit_pct, 2),
        'ma_info': ma,
        'actions': actions,
    }


def evaluate_all() -> List[Dict[str, Any]]:
    """扫所有持仓"""
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
            print(f'[position_profit_taker] {code} 扫描失败: {e}')
    out.sort(key=lambda x: (
        # critical > warning > info
        {'critical': 0, 'warning': 1, 'info': 2}.get(x['actions'][0]['severity'], 9),
        -x['profit_pct'],
    ))
    return out


def format_alert(items: List[Dict[str, Any]]) -> str:
    if not items:
        return '当前无减仓触发的持仓'
    critical = [x for x in items if any(a['severity'] == 'critical' for a in x['actions'])]
    warning = [x for x in items if any(a['severity'] == 'warning' for a in x['actions']) and x not in critical]
    info = [x for x in items if x not in critical and x not in warning]

    lines = [f'💰 持仓减仓信号扫描 — {datetime.now().strftime("%Y-%m-%d %H:%M")}',
             f'方案 A：30/60/100% 阶梯 + MA 趋势保护']

    if critical:
        lines.append(f'\n━━━ ⛔ 紧急清仓 ({len(critical)} 只) ━━━')
        for x in critical:
            for a in x['actions']:
                if a['severity'] == 'critical':
                    lines.append(_fmt_line(x, a))

    if warning:
        lines.append(f'\n━━━ ⚠️ 强烈建议减仓 ({len(warning)} 只) ━━━')
        for x in warning:
            for a in x['actions']:
                if a['severity'] == 'warning':
                    lines.append(_fmt_line(x, a))

    if info:
        lines.append(f'\n━━━ ✅ 阶梯减仓建议 ({len(info)} 只) ━━━')
        for x in info:
            for a in x['actions']:
                if a['severity'] == 'info':
                    lines.append(_fmt_line(x, a))

    return '\n'.join(lines)


def _fmt_line(x: Dict[str, Any], action: Dict[str, Any]) -> str:
    return (f"  • {x['symbol']} {x['name']}  ¥{x['current_price']:.2f}  "
            f"持仓盈亏 {x['profit_pct']:+.1f}%\n"
            f"     {action['reason']}\n"
            f"     👉 {action['recommendation']} (建议减 {action['action_pct']}%)")


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== position_profit_taker 自检 (方案 A) ===')
    items = evaluate_all()
    print(f'触发减仓信号: {len(items)} 只')
    print(format_alert(items))
