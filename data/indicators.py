# -*- coding: utf-8 -*-
"""data.indicators —— 技术指标计算(从 StockDataFetcher 拆出,2026-06-28 阶段3⑤)。

**这不是数据源**,是纯本地计算层:吃一根 K线 DataFrame(DatetimeIndex='Date' + 大写
Open/High/Low/Close/Volume),算出 MA/RSI/MACD/BOLL/KDJ + MyTT 通达信级指标(DMI/ATR/
TRIX/ROC/CCI/BIAS/WR),逐字段口径与原 StockDataFetcher 方法**完全一致**。

历史上这两函数挂在 `StockDataFetcher`(data/stock_data.py)上;源原子化重构把「源」与「指标计算」
分离 —— 源归 data/sources/*,指标计算归这里。`StockDataFetcher.calculate_technical_indicators` /
`get_latest_indicators` 现退化为对本模块的委托(6 处调用方零改),`datahub.kline_with_indicators`
直接调本模块。依赖:ta(移动平均/RSI/MACD/BOLL/KDJ)+ MyTT(纯 numpy 通达信指标,零额外依赖)。
"""
from __future__ import annotations

import ta


def calculate_technical_indicators(df):
    """计算技术指标。吃 K线 DataFrame(大写 OHLCV),原地加指标列后返回;
    入参是 {'error':...} 或异常时返回 error dict(与原 StockDataFetcher 方法行为一致)。"""
    try:
        if isinstance(df, dict) and "error" in df:
            return df

        # 移动平均线
        df['MA5'] = ta.trend.sma_indicator(df['Close'], window=5)
        df['MA10'] = ta.trend.sma_indicator(df['Close'], window=10)
        df['MA20'] = ta.trend.sma_indicator(df['Close'], window=20)
        df['MA60'] = ta.trend.sma_indicator(df['Close'], window=60)

        # RSI
        df['RSI'] = ta.momentum.rsi(df['Close'], window=14)

        # MACD
        macd = ta.trend.MACD(df['Close'])
        df['MACD'] = macd.macd()
        df['MACD_signal'] = macd.macd_signal()
        df['MACD_histogram'] = macd.macd_diff()

        # 布林带
        bollinger = ta.volatility.BollingerBands(df['Close'])
        df['BB_upper'] = bollinger.bollinger_hband()
        df['BB_middle'] = bollinger.bollinger_mavg()
        df['BB_lower'] = bollinger.bollinger_lband()

        # KDJ指标
        df['K'] = ta.momentum.stoch(df['High'], df['Low'], df['Close'])
        df['D'] = ta.momentum.stoch_signal(df['High'], df['Low'], df['Close'])

        # 成交量指标
        df['Volume_MA5'] = ta.trend.sma_indicator(df['Volume'], window=5)
        df['Volume_ratio'] = df['Volume'] / df['Volume_MA5']

        # 通达信级指标（MyTT，纯 numpy/pandas，零额外依赖）
        try:
            from MyTT import DMI, ATR, TRIX, ROC, CCI, BIAS, WR
            _close = df['Close'].values
            _high = df['High'].values
            _low = df['Low'].values

            # DMI 动向指标 — 趋势强度判断（ADX > 25 强趋势）
            pdi, mdi, adx, _ = DMI(_close, _high, _low)
            df['PDI'], df['MDI'], df['ADX'] = pdi, mdi, adx

            # ATR 真实波动 — 用于止损位计算
            df['ATR'] = ATR(_close, _high, _low, N=14)

            # TRIX 三重指数平滑 — 中长期趋势确认
            trix, trma = TRIX(_close)
            df['TRIX'], df['TRIX_MA'] = trix, trma

            # ROC 变动率 — 动量确认
            roc_val, roc_ma = ROC(_close, N=12, M=6)
            df['ROC'], df['ROC_MA'] = roc_val, roc_ma

            # CCI 顺势指标 — 超买超卖（±100）
            df['CCI'] = CCI(_close, _high, _low, N=14)

            # BIAS 乖离率（6/12/24 三周期）
            b6, b12, b24 = BIAS(_close)
            df['BIAS_6'], df['BIAS_12'], df['BIAS_24'] = b6, b12, b24

            # WR 威廉指标（短/中两周期）
            wr_s, wr_l = WR(_close, _high, _low)
            df['WR_10'], df['WR_6'] = wr_s, wr_l
        except Exception as _mytt_e:
            print(f"[MyTT] 通达信指标计算失败（不影响主流程）: {_mytt_e}")

        return df

    except Exception as e:
        return {"error": f"计算技术指标失败: {str(e)}"}


def get_latest_indicators(df):
    """获取最新一根的全部技术指标值(dict)。入参是 {'error':...} 或异常时返回 error dict。"""
    try:
        if isinstance(df, dict) and "error" in df:
            return df

        latest = df.iloc[-1]

        def _g(col, default=None):
            return latest[col] if col in df.columns else default

        return {
            "price": latest['Close'],
            "ma5": latest['MA5'],
            "ma10": latest['MA10'],
            "ma20": latest['MA20'],
            "ma60": latest['MA60'],
            "rsi": latest['RSI'],
            "macd": latest['MACD'],
            "macd_signal": latest['MACD_signal'],
            "bb_upper": latest['BB_upper'],
            "bb_lower": latest['BB_lower'],
            "k_value": latest['K'],
            "d_value": latest['D'],
            "volume_ratio": latest['Volume_ratio'],
            # MyTT 通达信级指标
            "pdi": _g('PDI'),
            "mdi": _g('MDI'),
            "adx": _g('ADX'),
            "atr": _g('ATR'),
            "trix": _g('TRIX'),
            "trix_ma": _g('TRIX_MA'),
            "roc": _g('ROC'),
            "roc_ma": _g('ROC_MA'),
            "cci": _g('CCI'),
            "bias_6": _g('BIAS_6'),
            "bias_12": _g('BIAS_12'),
            "bias_24": _g('BIAS_24'),
            "wr_10": _g('WR_10'),
        }
    except Exception as e:
        return {"error": f"获取最新指标失败: {str(e)}"}
