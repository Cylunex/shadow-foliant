"""
A股技术策略引擎 — 整合自 InStock(myhhub/stock) 的 8 大经典策略
+ 升级版放量验证 + 筹码分布

策略列表:
  1. platform_breakthrough   — 平台突破
  2. high_tight_flag         — 高而窄旗形
  3. keep_increasing         — 持续上涨(均线多头)
  4. low_atr_growth           — 低ATR成长
  5. low_backtrace_increase   — 无大幅回撤
  6. parking_apron            — 停机坪
  7. turtle_trade             — 海龟交易法则
  8. climax_limitdown         — 放量跌停

用法:
    from stock_strategies import StrategyEngine
    engine = StrategyEngine()
    results = engine.scan_all(df)  # df 含 date/open/high/low/close/volume/p_change 列
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import warnings

warnings.filterwarnings('ignore')

try:
    import talib as tl
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    print("[策略引擎] ⚠️ talib 未安装，部分策略不可用。安装: pip install TA-Lib")


class StrategyEngine:
    """A股技术策略扫描引擎"""

    # 总市值门槛（海龟策略用）
    MIN_MARKET_CAP = 200000

    def __init__(self):
        self.strategies = [
            ("platform_breakthrough", "平台突破", self.check_platform_breakthrough),
            ("high_tight_flag", "高而窄旗形", self.check_high_tight_flag),
            ("keep_increasing", "持续上涨(均线多头)", self.check_keep_increasing),
            ("low_atr_growth", "低ATR成长", self.check_low_atr_growth),
            ("low_backtrace_increase", "无大幅回撤", self.check_low_backtrace_increase),
            ("parking_apron", "停机坪", self.check_parking_apron),
            ("turtle_trade", "海龟交易法则", self.check_turtle_trade),
            ("climax_limitdown", "放量跌停(警示)", self.check_climax_limitdown),
        ]

    # ─── 公用: 放量上涨检测 ──────────────────────────────────

    def check_volume_breakout(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """
        检测放量上涨:
        1. 当日涨幅>=2% 且 收盘>开盘
        2. 成交额 >= 2亿
        3. 当日成交量 >= 5日均量 × 2
        """
        if not TALIB_AVAILABLE or len(data) < threshold:
            return False

        if date is not None:
            end_date = date.strftime("%Y-%m-%d")
            mask = (data['date'] <= end_date)
            data = data.loc[mask].copy()

        if len(data) < threshold:
            return False

        p_change = data.iloc[-1]['p_change']
        close = data.iloc[-1]['close']
        if p_change < 2 or close < data.iloc[-1]['open']:
            return False

        data.loc[:, 'vol_ma5'] = tl.MA(data['volume'].values.astype('float64'), timeperiod=5)
        data['vol_ma5'].values[np.isnan(data['vol_ma5'].values)] = 0.0

        data = data.tail(n=threshold + 1)
        if len(data) < threshold + 1:
            return False

        last_close = data.iloc[-1]['close']
        last_vol = data.iloc[-1]['volume']
        amount = last_close * last_vol

        if amount < 200000000:
            return False

        mean_vol = data.head(n=threshold).iloc[-1]['vol_ma5']
        vol_ratio = last_vol / mean_vol if mean_vol > 0 else 0

        return vol_ratio >= 2

    # ─── 公用: 海龟入场 ─────────────────────────────────────

    def check_turtle_enter(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """海龟入场: 当日收盘价 >= 最近N日最高收盘价"""
        if len(data) < threshold:
            return False
        if date is not None:
            end_date = date.strftime("%Y-%m-%d")
            mask = (data['date'] <= end_date)
            data = data.loc[mask]
        if len(data) < threshold:
            return False

        data = data.tail(n=threshold)
        max_price = data['close'].values.max()
        last_close = data.iloc[-1]['close']
        return last_close >= max_price

    # ═══════════════════════════════════════════════════════════
    #  策略 1: 平台突破
    # ═══════════════════════════════════════════════════════════

    def check_platform_breakthrough(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """
        平台突破策略:
        1. 60日内某日收盘价 >= 60日均线 > 开盘价
        2. 且该日放量上涨
        3. 且突破前任意一天收盘价与60日均线偏离在 -5%~20% 之间
        """
        if not TALIB_AVAILABLE or len(data) < threshold:
            return False

        origin_data = data.copy()
        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()

        if len(data) < threshold:
            return False

        data.loc[:, 'ma60'] = tl.MA(data['close'].values, timeperiod=60)
        data['ma60'] = data['ma60'].fillna(0.0)
        data = data.tail(n=threshold)

        breakthrough_row = None
        for idx in data.index:
            _close = data.loc[idx, 'close']
            _open = data.loc[idx, 'open']
            _date = data.loc[idx, 'date']
            _ma60 = data.loc[idx, 'ma60']
            if _open < _ma60 <= _close:
                d = datetime.strptime(str(_date)[:10], '%Y-%m-%d')
                if self.check_volume_breakout(origin_data, date=d.date(), threshold=threshold):
                    breakthrough_row = str(_date)[:10]
                    break

        if breakthrough_row is None:
            return False

        # 突破前，任意一天收盘价在60日均线的 -5%~20% 范围
        data_front = data.loc[(data['date'] < breakthrough_row) & (data['ma60'] > 0)]
        for _close, _ma60 in zip(data_front['close'].values, data_front['ma60'].values):
            if not (-0.05 < ((_ma60 - _close) / _ma60) < 0.2):
                return False

        return True

    # ═══════════════════════════════════════════════════════════
    #  策略 2: 高而窄旗形（需要龙虎榜配合）
    # ═══════════════════════════════════════════════════════════

    def check_high_tight_flag(self, data: pd.DataFrame, date=None,
                               threshold=60, on_dragon_tiger=False) -> bool:
        """
        高而窄旗形:
        1. 必须上市交易 >=60日
        2. 当日最高价 / 前24~10日最低价 >= 1.9
        3. 前24~10日必须连续两天涨幅 >= 9.5%
        4. [可选] 必须上龙虎榜
        """
        if on_dragon_tiger is False:
            return False

        if len(data) < threshold:
            return False

        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask]

        if len(data) < threshold:
            return False

        data = data.tail(n=24).head(n=14)
        low = data['low'].values.min()
        ratio_increase = data.iloc[-1]['high'] / low if low > 0 else 1
        if ratio_increase < 1.9:
            return False

        # 连续两天涨幅 >= 9.5%
        prev = 0.0
        for pct in data['p_change'].values:
            if pct >= 9.5:
                if prev >= 9.5:
                    return True
                prev = pct
            else:
                prev = 0.0
        return False

    # ═══════════════════════════════════════════════════════════
    #  策略 3: 持续上涨（均线多头）
    # ═══════════════════════════════════════════════════════════

    def check_keep_increasing(self, data: pd.DataFrame, date=None, threshold=30) -> bool:
        """
        持续上涨（MA30向上，均线多头）:
        1. 30日前MA30 < 20日前MA30 < 10日前MA30 < 当日MA30
        2. 当日MA30 / 30日前MA30 > 1.2
        """
        if not TALIB_AVAILABLE or len(data) < threshold:
            return False

        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()

        if len(data) < threshold:
            return False

        data.loc[:, 'ma30'] = tl.MA(data['close'].values, timeperiod=30)
        data['ma30'] = data['ma30'].fillna(0.0)
        data = data.tail(n=threshold)

        s1 = threshold // 3
        s2 = threshold * 2 // 3

        return (data.iloc[0]['ma30'] < data.iloc[s1]['ma30'] <
                data.iloc[s2]['ma30'] < data.iloc[-1]['ma30'] and
                data.iloc[-1]['ma30'] > 1.2 * data.iloc[0]['ma30'])

    # ═══════════════════════════════════════════════════════════
    #  策略 4: 低ATR成长
    # ═══════════════════════════════════════════════════════════

    def check_low_atr_growth(self, data: pd.DataFrame, date=None,
                              ma_long=250, threshold=10) -> bool:
        """
        低ATR成长:
        1. 上市交易 >=250日
        2. 最近10日最高收盘价/最低收盘价 >= 1.1
        3. ATR < 10（日振幅平均，排除剧烈波动股）
        """
        if len(data) < ma_long:
            return False

        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask]

        if len(data) < threshold:
            return False

        data = data.tail(n=threshold)

        total_change = 0.0
        highest = 0.0
        lowest = float('inf')

        for _close, _p_change in zip(data['close'].values, data['p_change'].values):
            total_change += abs(_p_change)
            if _close > highest:
                highest = _close
            if _close < lowest:
                lowest = _close

        atr = total_change / threshold
        if atr > 10:
            return False

        ratio = (highest - lowest) / lowest if lowest > 0 else 0
        return ratio > 1.1

    # ═══════════════════════════════════════════════════════════
    #  策略 5: 无大幅回撤
    # ═══════════════════════════════════════════════════════════

    def check_low_backtrace_increase(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """
        无大幅回撤:
        1. 60日涨幅 < 60%
        2. 60日内不允许: 单日跌>7% / 高开低走>7% / 两日累计跌>10%
        """
        if len(data) < threshold:
            return False

        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask]

        if len(data) < threshold:
            return False

        data = data.tail(n=threshold)
        ratio = (data.iloc[-1]['close'] - data.iloc[0]['close']) / data.iloc[0]['close']
        if ratio < 0.6:
            return False

        prev_pct = 100.0
        prev_open = -1000000.0
        for pct, close, opn in zip(data['p_change'].values,
                                    data['close'].values,
                                    data['open'].values):
            if opn == 0:
                continue
            gap = (close - opn) / opn * 100
            if (pct < -7 or gap < -7 or
                    prev_pct + pct < -10 or
                    (close - prev_open) / prev_open * 100 < -10):
                return False
            prev_pct = float(pct)
            prev_open = float(opn)
        return True

    # ═══════════════════════════════════════════════════════════
    #  策略 6: 停机坪
    # ═══════════════════════════════════════════════════════════

    def check_parking_apron(self, data: pd.DataFrame, date=None, threshold=15) -> bool:
        """
        停机坪:
        1. 最近15日有涨幅>9.5%且放量上涨（海龟入场）
        2. 下一个交易日高开，收盘价>开盘价，涨跌幅在 ±3%
        3. 之后2-3个交易日高开，收盘上涨，涨跌幅在 ±5%
        """
        origin_data = data.copy()
        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask]

        if len(data) < threshold:
            return False

        data = data.tail(n=threshold)

        # 找涨停+放量日
        for idx in range(len(data)):
            row = data.iloc[idx]
            _close = row.get('close', 0)
            _p_change = row.get('p_change', 0)
            _date = str(row.get('date', ''))
            if _p_change > 9.5:
                d = datetime.strptime(_date[:10], '%Y-%m-%d')
                if self.check_turtle_enter(origin_data, date=d.date(), threshold=threshold):
                    if self._check_parking_internal(data, _close, _date):
                        return True
        return False

    def _check_parking_internal(self, data, limitup_price, limitup_date):
        """停机坪内部验证: 涨停后3日形态"""
        tail = data.loc[data['date'] > limitup_date].head(3)
        if len(tail) < 3:
            return False

        d1 = tail.iloc[0]
        d23 = tail.tail(2)

        # 第1天: 高开，收盘>涨停价，振幅 3% 内
        if not (d1['close'] > limitup_price and d1['open'] > limitup_price and
                0.97 < d1['close'] / max(d1['open'], 0.01) < 1.03):
            return False

        # 第2-3天: 高开，收盘>涨停价，振幅 3%，涨跌幅 -5%~5%
        for _, row in d23.iterrows():
            if not (0.97 < row['close'] / max(row['open'], 0.01) < 1.03 and
                    -5 < row['p_change'] < 5 and
                    row['close'] > limitup_price and
                    row['open'] > limitup_price):
                return False

        return True

    # ═══════════════════════════════════════════════════════════
    #  策略 7: 海龟交易法则
    # ═══════════════════════════════════════════════════════════

    def check_turtle_trade(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """
        海龟交易法则:
        当日收盘价 >= 最近60日最高收盘价
        """
        return self.check_turtle_enter(data, date, threshold)

    # ═══════════════════════════════════════════════════════════
    #  策略 8: 放量跌停（风险警示）
    # ═══════════════════════════════════════════════════════════

    def check_climax_limitdown(self, data: pd.DataFrame, date=None, threshold=60) -> bool:
        """
        放量跌停（风险警示信号）:
        1. 跌幅 > 9.5%
        2. 成交额 >= 2亿
        3. 成交量 >= 5日均量 × 4
        """
        if not TALIB_AVAILABLE or len(data) < threshold:
            return False

        end_date = date.strftime("%Y-%m-%d") if date else data['date'].iloc[-1]
        mask = (data['date'] <= end_date)
        data = data.loc[mask].copy()

        if len(data) < threshold:
            return False

        if data.iloc[-1]['p_change'] > -9.5:
            return False

        data.loc[:, 'vol_ma5'] = tl.MA(data['volume'].values.astype('float64'), timeperiod=5)
        data['vol_ma5'] = data['vol_ma5'].fillna(0.0)

        data = data.tail(n=threshold + 1)
        if len(data) < threshold + 1:
            return False

        last_close = data.iloc[-1]['close']
        last_vol = data.iloc[-1]['volume']
        amount = last_close * last_vol

        if amount < 200000000:
            return False

        mean_vol = data.head(n=threshold).iloc[-1]['vol_ma5']
        vol_ratio = last_vol / max(mean_vol, 1)

        return vol_ratio >= 4

    # ═══════════════════════════════════════════════════════════
    #  批量扫描
    # ═══════════════════════════════════════════════════════════

    def scan_all(self, data: pd.DataFrame, date=None,
                 dragon_tiger=False) -> dict:
        """
        扫描所有策略，返回命中的策略列表。

        参数:
            data: DataFrame，至少含 date/open/high/low/close/volume/p_change 列
            date: 扫描日期（None=最新）
            dragon_tiger: 是否在龙虎榜上（影响高而窄旗形判定）

        返回:
            {strategy_id: {name, hit, signal}}
        """
        results = {}
        for sid, sname, check_func in self.strategies:
            try:
                if sid == "high_tight_flag":
                    hit = check_func(data, date=date,
                                     on_dragon_tiger=dragon_tiger)
                elif sid == "low_atr_growth":
                    hit = check_func(data, date=date, ma_long=250)
                elif sid == "parking_apron":
                    hit = check_func(data, date=date, threshold=15)
                else:
                    hit = check_func(data, date=date)

                signal = ""
                if hit:
                    if sid == "climax_limitdown":
                        signal = "⚠️ 放量跌停，风险警示"
                    elif sid in ("platform_breakthrough", "turtle_trade"):
                        signal = "🟢 突破买入信号"
                    elif sid in ("keep_increasing", "low_backtrace_increase", "low_atr_growth"):
                        signal = "🟢 趋势延续信号"
                    elif sid == "high_tight_flag":
                        signal = "🟢 强势旗形信号"
                    elif sid == "parking_apron":
                        signal = "🟡 盘整蓄力信号"

                results[sid] = {
                    "name": sname,
                    "hit": hit,
                    "signal": signal,
                }
            except Exception as e:
                results[sid] = {"name": sname, "hit": False, "signal": f"错误: {e}"}

        return results

    def scan_summary(self, data: pd.DataFrame, date=None,
                     dragon_tiger=False) -> list[dict]:
        """返回命中策略的简洁列表"""
        results = self.scan_all(data, date=date, dragon_tiger=dragon_tiger)
        hits = []
        for sid, info in results.items():
            if info["hit"]:
                hits.append({
                    "strategy_id": sid,
                    "name": info["name"],
                    "signal": info["signal"],
                })
        return hits


# ═══════════════════════════════════════════════════════════
# 筹码分布估算（简易版）
# ═══════════════════════════════════════════════════════════

def calc_chip_distribution(data: pd.DataFrame, window: int = 60) -> dict:
    """
    简易筹码分布估算

    参数:
        data: 含 close/volume 的 DataFrame
        window: 考察窗口

    返回:
        {profit_ratio: 获利比例, avg_cost: 平均成本,
         cost_percentile_25: P25成本, cost_percentile_75: P75成本,
         concentration: 筹码集中度}
    """
    chunk = data.tail(window).copy()
    if len(chunk) < window:
        return {}

    chunk['vwap'] = (chunk['close'] * chunk['volume']).cumsum() / chunk['volume'].cumsum()
    avg_cost = float(chunk['vwap'].iloc[-1])

    last_price = float(chunk['close'].iloc[-1])
    prices = chunk['close'].values
    profit_ratio = (prices > last_price).mean()

    p25 = float(np.percentile(prices, 25))
    p75 = float(np.percentile(prices, 75))
    concentration = (p75 - p25) / last_price * 100 if last_price > 0 else 100

    return {
        "last_price": last_price,
        "profit_ratio": round(profit_ratio * 100, 1),
        "loss_ratio": round((1 - profit_ratio) * 100, 1),
        "avg_cost": round(avg_cost, 2),
        "cost_range_low": round(p25, 2),
        "cost_range_high": round(p75, 2),
        "concentration_pct": round(concentration, 2),
        "window_days": window,
    }


# ═══════════════════════════════════════════════════════════
# 简易技术信号汇总
# ═══════════════════════════════════════════════════════════

def calc_trend_signals(data: pd.DataFrame) -> dict:
    """
    计算关键技术信号

    返回:
        {ma_cross, macd_signal, rsi_status, volume_ratio}
    """
    if not TALIB_AVAILABLE or len(data) < 60:
        return {}

    close = data['close'].values
    signals = {}

    # MA 交叉
    ma5 = tl.MA(close, timeperiod=5)
    ma20 = tl.MA(close, timeperiod=20)
    ma60 = tl.MA(close, timeperiod=60)
    if len(ma5) > 0 and len(ma20) > 0:
        # 金叉/死叉
        if ma5[-2] <= ma20[-2] and ma5[-1] > ma20[-1]:
            signals['ma_cross'] = 'golden_cross'  # 金叉
        elif ma5[-2] >= ma20[-2] and ma5[-1] < ma20[-1]:
            signals['ma_cross'] = 'death_cross'  # 死叉
        else:
            # 多头/空头排列
            if ma5[-1] > ma20[-1] > (ma60[-1] if len(ma60) > 0 else 0):
                signals['ma_cross'] = 'bullish_align'
            elif ma5[-1] < ma20[-1]:
                signals['ma_cross'] = 'bearish'
            else:
                signals['ma_cross'] = 'neutral'

    # MACD
    macd, macd_signal, macd_hist = tl.MACD(close)
    if len(macd_hist) >= 2:
        if macd_hist[-2] < 0 and macd_hist[-1] >= 0:
            signals['macd_signal'] = 'bullish_cross'
        elif macd_hist[-2] > 0 and macd_hist[-1] <= 0:
            signals['macd_signal'] = 'bearish_cross'
        elif macd_hist[-1] > 0:
            signals['macd_signal'] = 'bullish'
        else:
            signals['macd_signal'] = 'bearish'

    # RSI
    rsi = tl.RSI(close, timeperiod=14)
    if len(rsi) > 0:
        rsi_val = rsi[-1]
        if rsi_val >= 80:
            signals['rsi_status'] = 'overbought'
        elif rsi_val <= 20:
            signals['rsi_status'] = 'oversold'
        elif rsi_val >= 60:
            signals['rsi_status'] = 'bullish'
        elif rsi_val <= 40:
            signals['rsi_status'] = 'bearish'
        else:
            signals['rsi_status'] = 'neutral'
        signals['rsi_value'] = round(float(rsi_val), 1)

    # 量比
    vol_ma5 = tl.MA(data['volume'].values.astype('float64'), timeperiod=5) if 'volume' in data.columns else None
    if vol_ma5 is not None and len(vol_ma5) > 0 and vol_ma5[-1] > 0:
        vol_ratio = data["volume"].values.astype("float64")[-1] / vol_ma5[-1]
        if vol_ratio >= 2:
            signals['volume_ratio'] = 'heavy'
        elif vol_ratio >= 1.5:
            signals['volume_ratio'] = 'active'
        elif vol_ratio >= 0.8:
            signals['volume_ratio'] = 'normal'
        else:
            signals['volume_ratio'] = 'light'

    return signals


# ═══════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("Stock Strategies Engine V1.0")
    print("=" * 60)

    # 生成模拟数据
    np.random.seed(42)
    dates = pd.date_range(start='2025-01-01', periods=300, freq='B')
    close = 100 + np.cumsum(np.random.randn(300) * 0.5)
    open_p = close + np.random.randn(300) * 0.3
    high = np.maximum(close, open_p) + np.abs(np.random.randn(300)) * 0.5
    low = np.minimum(close, open_p) - np.abs(np.random.randn(300)) * 0.5
    volume = np.random.randint(1e7, 1e8, 300)
    p_change = (close / np.roll(close, 1) - 1) * 100
    p_change[0] = 0

    mock_data = pd.DataFrame({
        'date': dates,
        'open': open_p,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
        'p_change': p_change,
    })

    engine = StrategyEngine()
    results = engine.scan_all(mock_data)
    print("\n📊 策略扫描结果:")
    for sid, info in results.items():
        status = "✅ HIT" if info["hit"] else "  -"
        print(f"  {status} {info['name']}: {info['signal']}")

    # 筹码分布
    chip = calc_chip_distribution(mock_data)
    if chip:
        print(f"\n📦 筹码分布: 获利比例={chip['profit_ratio']}% "
              f"成本区间={chip['cost_range_low']}-{chip['cost_range_high']} "
              f"集中度={chip['concentration_pct']}%")

    # 趋势信号
    sig = calc_trend_signals(mock_data)
    if sig:
        print(f"\n📈 趋势信号: {sig}")

    print("\n✅ 策略引擎测试完成")
