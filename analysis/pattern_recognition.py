"""
K线形态识别引擎 — 纯计算复合形态 + 可选 TA-Lib 经典蜡烛形态

整合自 InStock(myhhub/stock) 和 FinceptTerminal 的模式检测方法

形态类别:
  - 反转形态: 锤子线, 倒锤子, 晨星, 黄昏星, 吞没, 十字星, 孕线等
  - 持续形态: 三白兵, 三乌鸦, 上升三法, 下降三法等
  - 特殊形态: 射击之星, 吊颈线, 前进受阻等
  - 复合形态: 双顶/双底, 头肩顶/底, 箱体, 旗形, 杯柄 (不依赖 TA-Lib)

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
    "hammer":               ("䭔子线",       "🔴看涨", tl.CDLHAMMER),
    "inverted_hammer":      ("倒锤子线",      "🔴看涨", tl.CDLINVERTEDHAMMER),
    "morning_star":         ("晨星",          "🔴看涨", tl.CDLMORNINGSTAR),
    "morning_doji_star":    ("晨星十字",      "🔴看涨", tl.CDLMORNINGDOJISTAR),
    "engulfing_bull":       ("看涨吞没",      "🔴看涨", tl.CDLENGULFING),
    "piercing":             ("刺透形态",      "🔴看涨", tl.CDLPIERCING),
    "harami_bull":          ("看涨孕线",      "🔴看涨", tl.CDLHARAMI),
    "harami_cross_bull":    ("看涨十字孕线",   "🔴看涨", tl.CDLHARAMICROSS),
    "three_white_soldiers": ("三白兵",        "🔴看涨", tl.CDL3WHITESOLDIERS),
    "three_inside_up":      ("三内升",        "🔴看涨", tl.CDL3INSIDE),
    "three_outside_up":     ("三外升",        "🔴看涨", tl.CDL3OUTSIDE),
    "dragonfly_doji":       ("蜻蜓十字",      "🔴看涨", tl.CDLDRAGONFLYDOJI),
    "abandoned_baby_bull":  ("看涨弃婴",      "🔴看涨", tl.CDLABANDONEDBABY),
    "tasukigap_bull":       ("向上跳空并列阳", "🔴看涨", tl.CDLTASUKIGAP),
    "breakaway_bull":       ("看涨脱离",      "🔴看涨", tl.CDLBREAKAWAY),
    "sticksandwich_bull":   ("看涨条形三明治", "🔴看涨", tl.CDLSTICKSANDWICH),
    "homing_pigeon":        ("家鸽",          "🔴看涨", tl.CDLHOMINGPIGEON),
    "matching_low":         ("匹配低位",      "🔴看涨", tl.CDLMATCHINGLOW),

    # === 看跌反转 ===
    "hanging_man":          ("吊颈线",        "🟢看跌", tl.CDLHANGINGMAN),
    "shooting_star":        ("射击之星",      "🟢看跌", tl.CDLSHOOTINGSTAR),
    "evening_star":         ("黄昏星",        "🟢看跌", tl.CDLEVENINGSTAR),
    "evening_doji_star":    ("黄昏十字星",    "🟢看跌", tl.CDLEVENINGDOJISTAR),
    "engulfing_bear":       ("看跌吞没",      "🟢看跌", tl.CDLENGULFING),
    "dark_cloud_cover":     ("乌云盖顶",      "🟢看跌", tl.CDLDARKCLOUDCOVER),
    "harami_bear":          ("看跌孕线",      "🟢看跌", tl.CDLHARAMI),
    "harami_cross_bear":    ("看跌十字孕线",   "🟢看跌", tl.CDLHARAMICROSS),
    "three_black_crows":    ("三乌鸦",        "🟢看跌", tl.CDL3BLACKCROWS),
    "three_inside_down":    ("三内降",        "🟢看跌", tl.CDL3INSIDE),
    "three_outside_down":   ("三外降",        "🟢看跌", tl.CDL3OUTSIDE),
    "gravestone_doji":      ("墓碑十字",      "🟢看跌", tl.CDLGRAVESTONEDOJI),
    "abandoned_baby_bear":  ("看跌弃婴",      "🟢看跌", tl.CDLABANDONEDBABY),
    "tasukigap_bear":       ("向下跳空并列阴", "🟢看跌", tl.CDLTASUKIGAP),
    "breakaway_bear":       ("看跌脱离",      "🟢看跌", tl.CDLBREAKAWAY),
    "advance_block":        ("前进受阻",      "🟢看跌", tl.CDLADVANCEBLOCK),
    "upside_gap_two_crows": ("向上跳空二乌鸦", "🟢看跌", tl.CDLUPSIDEGAP2CROWS),
    "two_crows":            ("双乌鸦",        "🟢看跌", tl.CDL2CROWS),
    "three_stars_south":    ("南方三星",      "🟢看跌", tl.CDL3STARSINSOUTH),
    "unique_3_river":       ("奇特三河床",    "🟢看跌", tl.CDLUNIQUE3RIVER),

    # === 中性/其他 ===
    "doji":                 ("十字星",        "⚪变盘", tl.CDLDOJI),
    "doji_star":            ("十字星形态",     "⚪变盘", tl.CDLDOJISTAR),
    "long_line":            ("长实体线",      "⚪研判", tl.CDLLONGLINE),
    "short_line":           ("短实体线",      "⚪观望", tl.CDLSHORTLINE),
    "spinning_top":         ("纺锤线",        "⚪观望", tl.CDLSPINNINGTOP),
    "marubozu":             ("光头光脚",      "⚪研判", tl.CDLMARUBOZU),
    "belt_hold":            ("捉腰带线",      "⚪研判", tl.CDLBELTHOLD),
    "rising_three_methods": ("上升三法",      "🔴持续", tl.CDLRISEFALL3METHODS),
    "falling_three_methods":("下降三法",      "🟢持续", tl.CDLRISEFALL3METHODS),
    "separating_lines":     ("分离线",        "⚪研判", tl.CDLSEPARATINGLINES),
    "conceal_baby_swallow": ("藏婴吞",        "⚪研判", tl.CDLCONCEALBABYSWALL),
    "ladder_bottom":        ("梯底",          "🔴看涨", tl.CDLLADDERBOTTOM),
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


def _pattern(name, direction, start_idx, end_idx, data, *, status="forming",
             breakout=None, invalidation=None, measured_target=None, strength="中"):
    """统一复合形态合同；只有 confirmed 才设置 found=True 供策略消费。"""
    type_text = "🔴看涨" if direction == "bullish" else "🟢看跌"
    date_value = data['date'].iloc[min(end_idx, len(data) - 1)]
    return {
        "name": name, "type": type_text, "direction": direction,
        "status": status, "found": status == "confirmed",
        "start_idx": int(start_idx), "end_idx": int(end_idx),
        "date": str(date_value)[:10], "strength": strength,
        "breakout_level": round(float(breakout), 2) if breakout is not None else None,
        "invalidation_level": round(float(invalidation), 2) if invalidation is not None else None,
        "measured_target": round(max(0.01, float(measured_target)), 2)
        if measured_target is not None else None,
    }


def detect_double_top_bottom(data: pd.DataFrame, window=3, tolerance=0.035) -> list:
    """双顶/双底检测，返回形成中/确认/失效状态以及颈线和测算目标。"""
    close = data['close'].values
    peaks, troughs = _find_local_extrema(close, window)
    patterns = []

    # 只取近期、最近的一组，避免同一段历史组合出大量重复形态。
    for points, name, direction in ((peaks, "双顶", "bearish"),
                                    (troughs, "双底", "bullish")):
        candidate = None
        for first, second in zip(points[:-1], points[1:]):
            if second < len(close) - 45 or second - first < window * 2:
                continue
            p1, p2 = float(close[first]), float(close[second])
            if p1 <= 0 or abs(p1 - p2) / p1 > tolerance:
                continue
            between = close[first:second + 1]
            pivot = float(between.min() if direction == "bearish" else between.max())
            depth = ((min(p1, p2) / pivot - 1) if direction == "bearish"
                     else (pivot / max(p1, p2) - 1))
            if depth < 0.05:
                continue
            current = float(close[-1])
            average = (p1 + p2) / 2
            if direction == "bearish":
                status = "confirmed" if current < pivot * 0.997 else (
                    "failed" if current > average * 1.035 else "forming")
                target, invalidation = pivot - (average - pivot), average * 1.02
            else:
                status = "confirmed" if current > pivot * 1.003 else (
                    "failed" if current < average * 0.965 else "forming")
                target, invalidation = pivot + (pivot - average), average * 0.98
            candidate = _pattern(name, direction, first, second, data, status=status,
                                 breakout=pivot, invalidation=invalidation,
                                 measured_target=target)
        if candidate:
            patterns.append(candidate)

    return patterns


def detect_box(data: pd.DataFrame, period=20, max_width=0.12) -> list:
    """箱体形成/突破；箱体边界严格取当前 K 线之前的 period 日。"""
    if len(data) < period + 1:
        return []
    close = data['close'].to_numpy(dtype=float)
    base = close[-period - 1:-1]
    bottom, top, current = float(base.min()), float(base.max()), float(close[-1])
    if bottom <= 0 or top / bottom - 1 > max_width:
        return []
    slope = float(np.polyfit(np.arange(period, dtype=float), base, 1)[0])
    drift = abs(slope * (period - 1)) / float(base.mean()) if float(base.mean()) else 1
    if drift > max_width * 0.5:  # 明显单边趋势不是箱体，交给趋势/旗形模块处理。
        return []
    direction, status = "bullish", "forming"
    if current > top * 1.005:
        direction, status = "bullish", "confirmed"
    elif current < bottom * 0.995:
        direction, status = "bearish", "confirmed"
    target = top + (top - bottom) if direction == "bullish" else bottom - (top - bottom)
    invalidation = bottom if direction == "bullish" else top
    return [_pattern("箱体突破" if status == "confirmed" else "箱体整理",
                     direction, len(data) - period - 1, len(data) - 1, data,
                     status=status, breakout=top if direction == "bullish" else bottom,
                     invalidation=invalidation, measured_target=target,
                     strength="强" if status == "confirmed" else "中")]


def detect_head_shoulders(data: pd.DataFrame, window=3) -> list:
    """检测最近三组局部极值构成的头肩/倒头肩结构。"""
    close = data['close'].to_numpy(dtype=float)
    peaks, troughs = _find_local_extrema(close, window)
    output = []
    for points, name, direction in ((peaks, "头肩顶", "bearish"),
                                    (troughs, "头肩底", "bullish")):
        if len(points) < 3:
            continue
        a, b, c = points[-3:]
        if a < len(close) - 70 or c < len(close) - 30 or c - a < window * 4:
            continue
        left, head, right = float(close[a]), float(close[b]), float(close[c])
        shoulders_close = abs(left - right) / max(abs(left), 1e-9) <= 0.06
        head_clear = (head > max(left, right) * 1.04 if direction == "bearish"
                      else head < min(left, right) * 0.96)
        if not shoulders_close or not head_clear:
            continue
        between1, between2 = close[a:b + 1], close[b:c + 1]
        neckline = float((between1.min() + between2.min()) / 2 if direction == "bearish"
                         else (between1.max() + between2.max()) / 2)
        current = float(close[-1])
        confirmed = current < neckline * 0.997 if direction == "bearish" else current > neckline * 1.003
        failed = current > head * 1.02 if direction == "bearish" else current < head * 0.98
        status = "confirmed" if confirmed else ("failed" if failed else "forming")
        height = abs(head - neckline)
        target = neckline - height if direction == "bearish" else neckline + height
        invalidation = max(left, right) * 1.02 if direction == "bearish" else min(left, right) * 0.98
        output.append(_pattern(name, direction, a, c, data, status=status,
                               breakout=neckline, invalidation=invalidation,
                               measured_target=target))
    return output


def detect_flag(data: pd.DataFrame) -> list:
    """简化旗形：显著旗杆后窄幅整理，当前突破整理区才确认。"""
    if len(data) < 31:
        return []
    close = data['close'].to_numpy(dtype=float)
    segment = close[-31:]
    pole_return = segment[10] / segment[0] - 1 if segment[0] else 0
    consolidation = segment[10:-1]
    width = consolidation.max() / consolidation.min() - 1 if consolidation.min() > 0 else 1
    if abs(pole_return) < 0.12 or width > 0.10:
        return []
    bullish = pole_return > 0
    boundary = float(consolidation.max() if bullish else consolidation.min())
    current = float(segment[-1])
    confirmed = current > boundary * 1.003 if bullish else current < boundary * 0.997
    failed = (current < float(consolidation.min()) * 0.997 if bullish
              else current > float(consolidation.max()) * 1.003)
    direction = "bullish" if bullish else "bearish"
    pole_height = abs(float(segment[10] - segment[0]))
    target = boundary + pole_height if bullish else boundary - pole_height
    invalidation = float(consolidation.min() if bullish else consolidation.max())
    return [_pattern("上升旗形" if bullish else "下降旗形", direction,
                     len(data) - 31, len(data) - 1, data,
                     status="confirmed" if confirmed else ("failed" if failed else "forming"),
                     breakout=boundary, invalidation=invalidation,
                     measured_target=target)]


def detect_cup_handle(data: pd.DataFrame) -> list:
    """保守杯柄检测：左右杯沿接近、杯底位于中段、末段回撤较浅。"""
    if len(data) < 61:
        return []
    close = data['close'].to_numpy(dtype=float)
    segment = close[-81:] if len(close) >= 81 else close[-61:]
    n = len(segment)
    left_end, right_start = max(8, int(n * 0.25)), int(n * 0.70)
    left_idx = int(np.argmax(segment[:left_end]))
    right_rel = int(np.argmax(segment[right_start:-1]))
    right_idx = right_start + right_rel
    if right_idx <= left_idx:
        return []
    left, right = float(segment[left_idx]), float(segment[right_idx])
    rim = (left + right) / 2
    bottom_idx = left_idx + int(np.argmin(segment[left_idx:right_idx + 1]))
    bottom = float(segment[bottom_idx])
    depth = 1 - bottom / rim if rim > 0 else 0
    handle = segment[right_idx:-1]
    handle_drawdown = 1 - float(handle.min()) / right if len(handle) and right > 0 else 0
    if abs(left - right) / max(left, 1e-9) > 0.08 or not 0.12 <= depth <= 0.40:
        return []
    if bottom_idx < int(n * 0.20) or bottom_idx > int(n * 0.75) or handle_drawdown > 0.12:
        return []
    current = float(segment[-1])
    confirmed = current > rim * 1.003
    failed = bool(len(handle) and current < float(handle.min()) * 0.997)
    return [_pattern("杯柄", "bullish", len(data) - n + left_idx, len(data) - 1, data,
                     status="confirmed" if confirmed else ("failed" if failed else "forming"), breakout=rim,
                     invalidation=float(handle.min()) if len(handle) else bottom,
                     measured_target=rim + (rim - bottom))]


# ═══════════════════════════════════════════════════════════
#  主检测器
# ═══════════════════════════════════════════════════════════

class PatternDetector:
    """K线形态检测器"""

    def __init__(self):
        # 复合形态是纯 pandas/numpy 实现，TA-Lib 只决定是否额外检测蜡烛形态。
        self.available = True
        self.talib_available = TALIB_AVAILABLE

    def detect_all(self, data: pd.DataFrame, date=None,
                   lookback: int = 5) -> dict:
        """
        检测所有K线形态

        返回:
            {pattern_id: {name, type, found, date, strength}}
        """
        if data is None or getattr(data, 'empty', True) or len(data) < 31:
            return {"error": "insufficient_data"}

        # 列名兼容：yfinance/akshare 风格大写 OHLC + 标准小写 + 中文
        df = data.copy()
        rename_map = {
            'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume',
            'Date': 'date', '日期': 'date', '开盘': 'open', '收盘': 'close',
            '最高': 'high', '最低': 'low', '成交量': 'volume',
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        # date 列若不存在则使用 index（即使 index 没有显式命名）。
        if 'date' not in df.columns:
            df['date'] = df.index
        # 截取到指定日期
        if date is not None and 'date' in df.columns:
            end_date = date.strftime("%Y-%m-%d")
            df = df.loc[df['date'].astype(str) <= end_date].copy()

        if len(df) < 31:
            return {"error": "insufficient_data"}

        for col in ('close',):
            if col not in df.columns:
                return {"error": f"missing_column: {col}"}

        for col in ('open', 'high', 'low', 'close', 'volume'):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['close'])
        close = df['close'].values.astype('float64')

        # 检测最近 lookback 天内的所有形态触发（取每个形态最近一次触发）
        results = {}
        lookback = min(lookback, len(df))
        if self.talib_available and all(col in df.columns for col in ('open', 'high', 'low')):
            open_p = df['open'].values.astype('float64')
            high = df['high'].values.astype('float64')
            low = df['low'].values.astype('float64')
            for pid, (name_cn, ptype, func) in TALIB_PATTERNS.items():
                try:
                    result = func(open_p, high, low, close)
                    recent = result[-lookback:]
                    last_idx = next((i for i in range(len(recent) - 1, -1, -1)
                                     if recent[i] != 0), None)
                    if last_idx is None:
                        continue
                    value = int(recent[last_idx])
                    # 同一个 TA-Lib 函数可能正负双向触发，避免“看涨吞没”收进负信号。
                    if ('🔴' in ptype and value < 0) or ('🟢' in ptype and value > 0):
                        continue
                    actual_offset = -(lookback - last_idx)
                    direction = ('bullish' if '🔴' in ptype else
                                 ('bearish' if '🟢' in ptype else 'neutral'))
                    results[pid] = {
                        "name": name_cn, "type": ptype, "direction": direction,
                        "status": "confirmed", "found": True,
                        "date": str(df['date'].iloc[actual_offset])[:10],
                        "value": value,
                        "strength": self._calc_strength(df, actual_offset, value),
                        "days_ago": lookback - 1 - last_idx,
                        "source": "talib",
                    }
                except Exception:
                    pass

        # 纯计算复合形态不依赖 TA-Lib；保留 forming 供 Agent 观察，只有 confirmed 才 found。
        detectors = (detect_double_top_bottom, detect_box, detect_head_shoulders,
                     detect_flag, detect_cup_handle)
        for detector in detectors:
            try:
                for index, pattern in enumerate(detector(df)):
                    pattern['source'] = 'composite'
                    results[f"composite_{detector.__name__}_{index}"] = pattern
            except Exception:
                pass

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
                if '🔴' in t:
                    bullish.append(entry)
                elif '🟢' in t:
                    bearish.append(entry)
                else:
                    neutral.append(entry)

            if bullish:
                lines.append(f"\n[🔴 看涨信号] ({len(bullish)}个):")
                for b in bullish[:4]:
                    lines.append(b)
            if bearish:
                lines.append(f"\n[🟢 看跌信号] ({len(bearish)}个):")
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
                if v.get('found') and '🔴' in str(v.get('type', ''))]

    def get_bearish_patterns(self, results: dict) -> list[str]:
        """提取看跌形态名称"""
        return [v['name'] for k, v in results.items()
                if v.get('found') and '🟢' in str(v.get('type', ''))]


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
