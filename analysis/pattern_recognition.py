"""
K线形态识别引擎 — 基于 TA-Lib 的 61 种经典形态识别

整合自 InStock(myhhub/stock) 和 FinceptTerminal 的模式检测方法

形态类别:
  - 反转形态: 锤子线, 倒锤子, 晨星, 黄昏星, 吞没, 十字星, 孕线等
  - 持续形态: 三白兵, 三乌鸦, 上升三法, 下降三法等
  - 特殊形态: 射击之星, 吊颈线, 前进受阻等
  - 复合形态: 双顶/双底, 头肩顶/底等 (基于局部极值检测)

用法:
    from pattern_recognition import PatternDetector
    detector = PatternDetector()
    patterns = detector.detect_all(df)
    summary = detector.format_patterns(patterns)
"""

import numpy as np
import pandas as pd
from typing import Optional

try:
    import talib as tl
    TALIB_AVAILABLE = True
except ImportError:
    TALIB_AVAILABLE = False
    # 提供 dummy 占位，让顶层 PATTERNS dict 字面值能正常构建
    # 实际形态识别在 TALIB_AVAILABLE 为 False 时由调用方降级
    class _StubTalib:
        def __getattr__(self, name):
            return lambda *a, **kw: None
    tl = _StubTalib()


# ═══════════════════════════════════════════════════════════
#  TA-Lib 形态映射
# ═══════════════════════════════════════════════════════════

TALIB_PATTERNS = {
    # === 看涨反转 ===
    "hammer":               ("䭔子线",       "🟢看涨", tl.CDLHAMMER),
    "inverted_hammer":      ("倒锤子线",      "🟢看涨", tl.CDLINVERTEDHAMMER),
    "morning_star":         ("晨星",          "🟢看涨", tl.CDLMORNINGSTAR),
    "morning_doji_star":    ("晨星十字",      "🟢看涨", tl.CDLMORNINGDOJISTAR),
    "engulfing_bull":       ("看涨吞没",      "🟢看涨", tl.CDLENGULFING),
    "piercing":             ("刺透形态",      "🟢看涨", tl.CDLPIERCING),
    "harami_bull":          ("看涨孕线",      "🟢看涨", tl.CDLHARAMI),
    "harami_cross_bull":    ("看涨十字孕线",   "🟢看涨", tl.CDLHARAMICROSS),
    "three_white_soldiers": ("三白兵",        "🟢看涨", tl.CDL3WHITESOLDIERS),
    "three_inside_up":      ("三内升",        "🟢看涨", tl.CDL3INSIDE),
    "three_outside_up":     ("三外升",        "🟢看涨", tl.CDL3OUTSIDE),
    "dragonfly_doji":       ("蜻蜓十字",      "🟢看涨", tl.CDLDRAGONFLYDOJI),
    "abandoned_baby_bull":  ("看涨弃婴",      "🟢看涨", tl.CDLABANDONEDBABY),
    "tasukigap_bull":       ("向上跳空并列阳", "🟢看涨", tl.CDLTASUKIGAP),
    "breakaway_bull":       ("看涨脱离",      "🟢看涨", tl.CDLBREAKAWAY),
    "sticksandwich_bull":   ("看涨条形三明治", "🟢看涨", tl.CDLSTICKSANDWICH),
    "homing_pigeon":        ("家鸽",          "🟢看涨", tl.CDLHOMINGPIGEON),
    "matching_low":         ("匹配低位",      "🟢看涨", tl.CDLMATCHINGLOW),

    # === 看跌反转 ===
    "hanging_man":          ("吊颈线",        "🔴看跌", tl.CDLHANGINGMAN),
    "shooting_star":        ("射击之星",      "🔴看跌", tl.CDLSHOOTINGSTAR),
    "evening_star":         ("黄昏星",        "🔴看跌", tl.CDLEVENINGSTAR),
    "evening_doji_star":    ("黄昏十字星",    "🔴看跌", tl.CDLEVENINGDOJISTAR),
    "engulfing_bear":       ("看跌吞没",      "🔴看跌", tl.CDLENGULFING),
    "dark_cloud_cover":     ("乌云盖顶",      "🔴看跌", tl.CDLDARKCLOUDCOVER),
    "harami_bear":          ("看跌孕线",      "🔴看跌", tl.CDLHARAMI),
    "harami_cross_bear":    ("看跌十字孕线",   "🔴看跌", tl.CDLHARAMICROSS),
    "three_black_crows":    ("三乌鸦",        "🔴看跌", tl.CDL3BLACKCROWS),
    "three_inside_down":    ("三内降",        "🔴看跌", tl.CDL3INSIDE),
    "three_outside_down":   ("三外降",        "🔴看跌", tl.CDL3OUTSIDE),
    "gravestone_doji":      ("墓碑十字",      "🔴看跌", tl.CDLGRAVESTONEDOJI),
    "abandoned_baby_bear":  ("看跌弃婴",      "🔴看跌", tl.CDLABANDONEDBABY),
    "tasukigap_bear":       ("向下跳空并列阴", "🔴看跌", tl.CDLTASUKIGAP),
    "breakaway_bear":       ("看跌脱离",      "🔴看跌", tl.CDLBREAKAWAY),
    "advance_block":        ("前进受阻",      "🔴看跌", tl.CDLADVANCEBLOCK),
    "upside_gap_two_crows": ("向上跳空二乌鸦", "🔴看跌", tl.CDLUPSIDEGAP2CROWS),
    "two_crows":            ("双乌鸦",        "🔴看跌", tl.CDL2CROWS),
    "three_stars_south":    ("南方三星",      "🔴看跌", tl.CDL3STARSINSOUTH),
    "unique_3_river":       ("奇特三河床",    "🔴看跌", tl.CDLUNIQUE3RIVER),

    # === 中性/其他 ===
    "doji":                 ("十字星",        "⚪变盘", tl.CDLDOJI),
    "doji_star":            ("十字星形态",     "⚪变盘", tl.CDLDOJISTAR),
    "long_line":            ("长实体线",      "⚪研判", tl.CDLLONGLINE),
    "short_line":           ("短实体线",      "⚪观望", tl.CDLSHORTLINE),
    "spinning_top":         ("纺锤线",        "⚪观望", tl.CDLSPINNINGTOP),
    "marubozu":             ("光头光脚",      "⚪研判", tl.CDLMARUBOZU),
    "belt_hold":            ("捉腰带线",      "⚪研判", tl.CDLBELTHOLD),
    "rising_three_methods": ("上升三法",      "🟢持续", tl.CDLRISEFALL3METHODS),
    "falling_three_methods":("下降三法",      "🔴持续", tl.CDLRISEFALL3METHODS),
    "separating_lines":     ("分离线",        "⚪研判", tl.CDLSEPARATINGLINES),
    "conceal_baby_swallow": ("藏婴吞",        "⚪研判", tl.CDLCONCEALBABYSWALL),
    "ladder_bottom":        ("梯底",          "🟢看涨", tl.CDLLADDERBOTTOM),
    "kicking":              ("反冲形态",      "⚪研判", tl.CDLKICKING),
    "high_wave":            ("大浪线",        "⚪观望", tl.CDLHIGHWAVE),
    "counter_attack":       ("反击线",        "⚪研判", tl.CDLCOUNTERATTACK),
}


# ═══════════════════════════════════════════════════════════
#  复合形态: 局部极值检测
# ═══════════════════════════════════════════════════════════

def _find_local_extrema(prices, window=5):
    """检测局部极值点"""
    peaks, troughs = [], []
    n = len(prices)
    for i in range(window, n - window):
        left = prices[i - window:i]
        right = prices[i + 1:i + window + 1]
        if prices[i] > left.max() and prices[i] > right.max():
            peaks.append(i)
        elif prices[i] < left.min() and prices[i] < right.min():
            troughs.append(i)
    return peaks, troughs


def detect_double_top_bottom(data: pd.DataFrame, window=5, tolerance=0.03) -> list:
    """双顶/双底检测"""
    close = data['close'].values
    peaks, troughs = _find_local_extrema(close, window)
    patterns = []

    # 双顶
    for i in range(len(peaks) - 1):
        for j in range(i + 1, len(peaks)):
            diff = abs(close[peaks[i]] - close[peaks[j]]) / close[peaks[i]]
            if diff < tolerance and peaks[j] - peaks[i] >= window:
                valley_between = close[peaks[i]:peaks[j] + 1].min()
                if valley_between < min(close[peaks[i]], close[peaks[j]]) * 0.95:
                    patterns.append({
                        "name": "双顶",
                        "type": "🔴看跌",
                        "start_idx": peaks[i],
                        "end_idx": peaks[j],
                        "date": str(data['date'].iloc[peaks[j]])[:10],
                    })

    # 双底
    for i in range(len(troughs) - 1):
        for j in range(i + 1, len(troughs)):
            diff = abs(close[troughs[i]] - close[troughs[j]]) / close[troughs[i]]
            if diff < tolerance and troughs[j] - troughs[i] >= window:
                peak_between = close[troughs[i]:troughs[j] + 1].max()
                if peak_between > max(close[troughs[i]], close[troughs[j]]) * 1.05:
                    patterns.append({
                        "name": "双底",
                        "type": "🟢看涨",
                        "start_idx": troughs[i],
                        "end_idx": troughs[j],
                        "date": str(data['date'].iloc[troughs[j]])[:10],
                    })

    return patterns


# ═══════════════════════════════════════════════════════════
#  主检测器
# ═══════════════════════════════════════════════════════════

class PatternDetector:
    """K线形态检测器"""

    def __init__(self):
        self.available = TALIB_AVAILABLE
        if not self.available:
            print("[形态检测] ⚠️ talib 未安装，形态检测不可用。pip install TA-Lib")

    def detect_all(self, data: pd.DataFrame, date=None,
                   lookback: int = 5) -> dict:
        """
        检测所有K线形态

        返回:
            {pattern_id: {name, type, found, date, strength}}
        """
        if not self.available:
            return {"error": "talib_not_available"}

        if len(data) < 120:
            return {"error": "insufficient_data"}

        # 列名兼容：yfinance/akshare 风格大写 OHLC + 标准小写 + 中文
        df = data.copy()
        rename_map = {
            'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume',
            'Date': 'date', '日期': 'date', '开盘': 'open', '收盘': 'close',
            '最高': 'high', '最低': 'low', '成交量': 'volume',
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        # date 列若是 index 则下沉
        if 'date' not in df.columns and df.index.name in ('Date', 'date', '日期'):
            df = df.reset_index().rename(columns={df.index.name: 'date'})
        # 截取到指定日期
        if date is not None and 'date' in df.columns:
            end_date = date.strftime("%Y-%m-%d")
            df = df.loc[df['date'].astype(str) <= end_date].copy()

        if len(df) < 120:
            return {"error": "insufficient_data"}

        for col in ('open', 'high', 'low', 'close'):
            if col not in df.columns:
                return {"error": f"missing_column: {col}"}

        open_p = df['open'].values.astype('float64')
        high = df['high'].values.astype('float64')
        low = df['low'].values.astype('float64')
        close = df['close'].values.astype('float64')

        # 检测最近 lookback 天内的所有形态触发（取每个形态最近一次触发）
        results = {}
        lookback = min(lookback, len(df))
        for pid, (name_cn, ptype, func) in TALIB_PATTERNS.items():
            try:
                result = func(open_p, high, low, close)
                recent = result[-lookback:]
                last_idx = None
                for i in range(len(recent) - 1, -1, -1):
                    if recent[i] != 0:
                        last_idx = i
                        break
                if last_idx is None:
                    continue
                actual_offset = -(lookback - last_idx)
                strength = self._calc_strength(df, actual_offset, recent[last_idx])
                date_str = str(df['date'].iloc[actual_offset])[:10]

                results[pid] = {
                    "name": name_cn,
                    "type": ptype,
                    "found": True,
                    "date": date_str,
                    "value": int(recent[last_idx]),
                    "strength": strength,
                    "days_ago": lookback - 1 - last_idx,
                }
            except Exception:
                pass

        # 复合形态
        double_patterns = detect_double_top_bottom(df)
        for i, dp in enumerate(double_patterns):
            if dp['date'] == (str(df['date'].iloc[-1])[:10] if len(df) > 0 else ''):
                results[f"double_{i}"] = {
                    "name": dp['name'],
                    "type": dp['type'],
                    "found": True,
                    "date": dp['date'],
                    "value": 100,
                    "strength": "中",
                }

        # 加上支撑/阻力位
        try:
            support_resistance = self._calc_support_resistance(close)
            results["support_resistance"] = support_resistance
        except Exception:
            pass

        return results

    def _calc_strength(self, data: pd.DataFrame, offset, value) -> str:
        """计算形态强度"""
        abs_val = abs(value)
        # 配合成交量判断
        try:
            vol = data['volume'].values[offset]
            vol_ma20 = data['volume'].values[-20:].mean()
            vol_factor = vol / vol_ma20 if vol_ma20 > 0 else 1
        except (IndexError, KeyError):
            vol_factor = 1

        if abs_val >= 100 and vol_factor >= 2:
            return "强"
        elif abs_val >= 100 or vol_factor >= 2:
            return "中"
        else:
            return "弱"

    def _calc_support_resistance(self, close: np.ndarray) -> dict:
        """计算支撑位和阻力位"""
        if len(close) < 60:
            return {"found": False}

        recent = close[-60:]
        recent_high = float(recent.max())
        recent_low = float(recent.min())
        current = float(close[-1])

        # 简单支撑/阻力（基于近期高低点）
        supports = []
        resistances = []

        # 20日均线
        ma20 = float(np.mean(close[-20:]))
        if current > ma20:
            supports.append({"level": round(ma20, 2), "type": "MA20支撑"})
        else:
            resistances.append({"level": round(ma20, 2), "type": "MA20阻力"})

        # 60日均线
        if len(close) >= 60:
            ma60 = float(np.mean(close[-60:]))
            if current > ma60:
                supports.append({"level": round(ma60, 2), "type": "MA60支撑"})
            else:
                resistances.append({"level": round(ma60, 2), "type": "MA60阻力"})

        # 布林带
        if TALIB_AVAILABLE and len(close) >= 20:
            upper, middle, lower = tl.BBANDS(close, timeperiod=20)
            if len(upper) > 0:
                resistances.append({
                    "level": round(float(upper[-1]), 2),
                    "type": "布林上轨"
                })
                supports.append({
                    "level": round(float(lower[-1]), 2),
                    "type": "布林下轨"
                })

        return {
            "found": True,
            "current_price": round(current, 2),
            "supports": sorted(supports, key=lambda x: x['level'], reverse=True),
            "resistances": sorted(resistances, key=lambda x: x['level']),
            "recent_high": round(recent_high, 2),
            "recent_low": round(recent_low, 2),
        }

    def format_patterns(self, results: dict, max_display: int = 8) -> str:
        """将检测结果格式化为可读文本"""
        if isinstance(results, dict) and "error" in results:
            return f"⚠️ 形态检测不可用: {results['error']}"

        hits = {k: v for k, v in results.items()
                if v.get("found") and k != "support_resistance"}

        lines = ["═══════════════════════════════════════",
                  "【K线形态识别】",
                  "═══════════════════════════════════════"]

        if not hits:
            lines.append("  近期无明显形态信号")
        else:
            # 按类型分组
            bullish = []
            bearish = []
            neutral = []
            for pid, info in hits.items():
                t = info.get('type', '')
                entry = f"  {t} {info['name']} (强度:{info.get('strength','?')})"
                if '🟢' in t:
                    bullish.append(entry)
                elif '🔴' in t:
                    bearish.append(entry)
                else:
                    neutral.append(entry)

            if bullish:
                lines.append(f"\n[🟢 看涨信号] ({len(bullish)}个):")
                for b in bullish[:4]:
                    lines.append(b)
            if bearish:
                lines.append(f"\n[🔴 看跌信号] ({len(bearish)}个):")
                for b in bearish[:4]:
                    lines.append(b)
            if neutral:
                lines.append(f"\n[⚪ 其他信号] ({len(neutral)}个):")
                for n in neutral[:4]:
                    lines.append(n)

        # 支撑/阻力
        sr = results.get("support_resistance", {})
        if sr.get("found"):
            lines.append("\n───────────────────────────────────────")
            lines.append(f"[📊 关键价位] 现价: {sr['current_price']}")
            if sr.get("resistances"):
                lines.append("  阻力位:")
                for r in sr["resistances"][:3]:
                    lines.append(f"    {r['type']}: {r['level']}")
            if sr.get("supports"):
                lines.append("  支撑位:")
                for s in sr["supports"][:3]:
                    lines.append(f"    {s['type']}: {s['level']}")

        lines.append("═══════════════════════════════════════")
        return "\n".join(lines)

    def get_bullish_patterns(self, results: dict) -> list[str]:
        """提取看涨形态名称"""
        return [v['name'] for k, v in results.items()
                if v.get('found') and '🟢' in str(v.get('type', ''))]

    def get_bearish_patterns(self, results: dict) -> list[str]:
        """提取看跌形态名称"""
        return [v['name'] for k, v in results.items()
                if v.get('found') and '🔴' in str(v.get('type', ''))]


# ═══════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("K线形态识别引擎 V1.0")
    print("=" * 60)

    # 生成模拟K线数据
    np.random.seed(42)
    dates = pd.date_range(start='2025-06-01', periods=150, freq='B')
    close = 50 + np.cumsum(np.random.randn(150) * 0.3)
    open_p = close + np.random.randn(150) * 0.2
    high = np.maximum(close, open_p) + np.abs(np.random.randn(150)) * 0.3
    low = np.minimum(close, open_p) - np.abs(np.random.randn(150)) * 0.3
    volume = np.random.randint(1e6, 5e7, 150)

    mock_data = pd.DataFrame({
        'date': dates,
        'open': open_p,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
    })

    detector = PatternDetector()
    if detector.available:
        results = detector.detect_all(mock_data)
        formatted = detector.format_patterns(results)
        print(formatted)

        bullish = detector.get_bullish_patterns(results)
        bearish = detector.get_bearish_patterns(results)
        print(f"\n看涨形态: {bullish}")
        print(f"看跌形态: {bearish}")
    else:
        print("⚠️ TA-Lib 未安装，无法演示。请执行: pip install TA-Lib")

    print("\n✅ 形态识别引擎测试完成")
