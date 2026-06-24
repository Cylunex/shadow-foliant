"""持仓 4 象限自动分级

设计目标：30+ 只持仓你看不过来 → 系统自动把每只股归到 4 类：
  🟢 健康   基本面 ≥ B 级 且 趋势向上 且 未破位
  🟡 观察   基本面 C 级 或 持仓跌幅 [10%, 25%]
  🔴 警报   基本面 D/E 或 持仓跌幅 > 25% 或 出现高位看跌反转形态
  ⚪ N/A    数据不足 / 持仓 0 股

接口：
  classify_one(symbol, stock_info=None)
  classify_all() -> {'healthy': [...], 'watch': [...], 'alert': [...], 'na': [...]}
  format_report(by_class) -> str
"""

import os
from typing import Dict, List, Any, Optional

import user_strategy_config as cfg
from position_guardian import _portfolio_db, _get_current_price


REVERSAL_BEARISH = {
    'evening_star', 'evening_doji_star', 'abandoned_baby_bear',
    'three_black_crows', 'engulfing_bear', 'shooting_star',
    'hanging_man', 'dark_cloud_cover', 'two_crows', 'advance_block',
    'gravestone_doji',
}


def _check_trend_and_pattern(symbol: str) -> Dict[str, Any]:
    """跑 TA-Lib 形态 + 简易趋势（MA20 vs MA60）"""
    result: Dict[str, Any] = {
        'trend_up': None,
        'breakout_down': False,
        'bearish_patterns': [],
    }
    df = None
    try:
        from stock_data import StockDataFetcher
        df = StockDataFetcher().get_stock_data(symbol, '1y', adjust='qfq')   # 趋势+形态共用,前复权
        if df is None or len(df) < 120:
            return result
        close_col = 'Close' if 'Close' in df.columns else 'close'
        closes = df[close_col].astype('float64').values
        ma20 = closes[-20:].mean()
        ma60 = closes[-60:].mean() if len(closes) >= 60 else None
        last = closes[-1]
        if ma60:
            result['trend_up'] = bool((ma20 > ma60) and (last >= ma20 * 0.97))   # 转 python bool(原 np.bool_ 不可 JSON 序列化)
            result['breakout_down'] = bool(last < ma60 * 0.95)
    except Exception:
        pass

    try:
        from pattern_recognition import PatternDetector
        det = PatternDetector()
        if det.available and df is not None and len(df) >= 120:
            r = det.detect_all(df, lookback=3)
            for pid, info in r.items():
                if pid == 'support_resistance' or not isinstance(info, dict):
                    continue
                if info.get('found') and pid in REVERSAL_BEARISH and info.get('days_ago', 99) <= 2:
                    result['bearish_patterns'].append(f"{info.get('name', pid)}")
    except Exception:
        pass

    return result


def classify_one(symbol: str, stock_info: Optional[Dict] = None,
                 with_fundamental: bool = True) -> Dict[str, Any]:
    """对单股归类。返回必有 class 字段：healthy / watch / alert / na。
    with_fundamental=False 跳过基本面评分(score_one 每只 2-4s 且依赖 pywencai/F10 抓取,
    弱网/未配置时返回 N/A 纯耗时)→ 仅用 持仓盈亏 + 趋势/形态 快速归类。"""
    pdb = _portfolio_db()
    if stock_info is None:
        stock_info = pdb.get_stock_by_code(symbol)
    if not stock_info:
        return {'symbol': symbol, 'class': 'na', 'reason': 'not_in_portfolio'}

    name = stock_info.get('name', '')
    cost_price = float(stock_info.get('cost_price', 0) or 0)
    qty = float(stock_info.get('quantity', 0) or 0)
    if cost_price <= 0 or qty <= 0:
        return {'symbol': symbol, 'name': name, 'class': 'na',
                'reason': 'no_position'}

    cur = _get_current_price(symbol)
    if cur is None or cur <= 0:
        return {'symbol': symbol, 'name': name, 'class': 'na',
                'reason': 'no_price'}

    holding_pnl_pct = (cur - cost_price) / cost_price * 100

    fund_score, fund_grade = None, 'N/A'
    if with_fundamental:
        try:
            from fundamental_scoring import score_one
            fund = score_one(symbol) or {}
            fund_score = fund.get('score')
            fund_grade = fund.get('grade', 'N/A')
        except Exception:
            pass

    tech = _check_trend_and_pattern(symbol)

    obs_low = abs(cfg.get('observation_drop_low', 10.0))
    obs_high = abs(cfg.get('observation_drop_high', 25.0))

    fund_grade_letter = (fund_grade or 'N/A')[:1]
    is_de = fund_grade_letter in ('D', 'E')

    klass = None
    reasons: List[str] = []

    if is_de:
        klass = 'alert'
        reasons.append(f'基本面 {fund_grade}')
    if holding_pnl_pct <= -obs_high:
        klass = 'alert'
        reasons.append(f'跌幅 {holding_pnl_pct:.1f}% 超 {obs_high}%')
    if tech['bearish_patterns']:
        klass = 'alert'
        reasons.append(f'看跌反转形态: {", ".join(tech["bearish_patterns"])}')
    if tech.get('breakout_down'):
        klass = klass or 'alert'
        reasons.append('跌破 MA60 趋势线')

    if klass is None:
        if -obs_high < holding_pnl_pct <= -obs_low:
            klass = 'watch'
            reasons.append(f'跌幅 {holding_pnl_pct:.1f}% 在观察区间')
        elif fund_grade_letter == 'C':
            klass = 'watch'
            reasons.append('基本面 C 级')

    if klass is None:
        klass = 'healthy'
        if tech.get('trend_up'):
            reasons.append('趋势向上')
        if fund_grade_letter in ('A', 'B'):
            reasons.append(f'基本面 {fund_grade}')

    return {
        'symbol': symbol,
        'name': name,
        'class': klass,
        'reasons': reasons,
        'cost_price': cost_price,
        'current_price': cur,
        'quantity': qty,
        'position_value': round(cur * qty, 2),
        'holding_pnl_pct': round(holding_pnl_pct, 2),
        'fundamental_grade': fund_grade,
        'fundamental_score': fund_score,
        'trend_up': tech.get('trend_up'),
        'breakout_down': tech.get('breakout_down'),
        'bearish_patterns': tech.get('bearish_patterns'),
    }


def classify_all(max_workers: int = 8, limit: int = 0,
                 with_fundamental: bool = True) -> Dict[str, List[Dict[str, Any]]]:
    """扫持仓分类汇总。每只独立(基本面评分+K线形态,含外部抓取)→ 并发跑。
    limit>0 时只扫**市值最大的前 N 只**;with_fundamental=False 跳过慢的基本面评分(见 classify_one)。"""
    from concurrent.futures import ThreadPoolExecutor
    pdb = _portfolio_db()
    stocks = [s for s in (pdb.get_all_stocks() or []) if s.get('code')]
    if limit and limit > 0 and len(stocks) > limit:
        stocks.sort(key=lambda s: float(s.get('cost_price') or 0) * float(s.get('quantity') or 0), reverse=True)
        stocks = stocks[:limit]
    by_class: Dict[str, List] = {'healthy': [], 'watch': [], 'alert': [], 'na': []}

    def _one(s):
        code = s.get('code')
        try:
            return classify_one(code, stock_info=s, with_fundamental=with_fundamental)
        except Exception as e:
            return {'symbol': code, 'name': s.get('name', ''), 'class': 'na', 'reason': str(e)}

    if stocks:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(stocks))) as ex:
            for r in ex.map(_one, stocks):
                by_class.get(r.get('class', 'na'), by_class['na']).append(r)
    return by_class


def format_report(by_class: Dict[str, List[Dict[str, Any]]]) -> str:
    lines = []
    labels = {'healthy': '🟢 健康', 'watch': '🟡 观察', 'alert': '🔴 警报', 'na': '⚪ 数据不足'}
    for k in ('alert', 'watch', 'healthy', 'na'):
        items = by_class.get(k, [])
        if not items:
            continue
        lines.append(f'\n━━━ {labels[k]} ({len(items)} 只) ━━━')
        for x in items:
            if k == 'na':
                lines.append(f"  • {x.get('symbol')} {x.get('name','')}  原因: {x.get('reason', '?')}")
                continue
            sym = x.get('symbol')
            nm = x.get('name', '')
            pnl = x.get('holding_pnl_pct', 0)
            grade = x.get('fundamental_grade', '?')
            why = '; '.join(x.get('reasons', []))[:120]
            lines.append(f"  • {sym} {nm}  {pnl:+.1f}%  {grade}  — {why}")
    return '\n'.join(lines).strip()


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== portfolio_classifier 自检 ===')
    by = classify_all()
    print(f"健康={len(by['healthy'])} 观察={len(by['watch'])} 警报={len(by['alert'])} N/A={len(by['na'])}")
    print(format_report(by))
