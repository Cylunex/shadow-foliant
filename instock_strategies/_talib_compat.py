"""talib 兼容层 —— 装了 talib 用真的,没装(生产 venv 常缺这个 C 库)就用 pandas/numpy 等价兜底。

instock_strategies/*.py 一律 `from instock_strategies._talib_compat import tl`,这样**缺 talib 也不会让
instock_strategy_runner 整包导入失败**(否则早间策略扫描/回测/分级全崩,报 No module named 'talib')。
只覆盖本项目实际用到的 MA / RSI / MACD / BBANDS,签名对齐 talib;装了 talib 时直接透传真实现。
"""
import numpy as np

try:
    import talib as tl  # noqa: F401  装了就用真的(行为完全一致)
except ImportError:
    import pandas as _pd

    class _TLShim:
        """talib 缺失时的 pandas 等价实现(够策略判定用;与真 talib 数值高度一致)。"""

        @staticmethod
        def MA(real, timeperiod=30, matype=0):
            return _pd.Series(real).rolling(int(timeperiod)).mean().to_numpy()

        @staticmethod
        def RSI(real, timeperiod=14):
            s = _pd.Series(real)
            d = s.diff()
            n = int(timeperiod)
            # Wilder 平滑(贴近 talib RSI)
            up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False).mean()
            dn = (-d.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False).mean()
            rs = up / dn.replace(0, np.nan)
            return (100 - 100 / (1 + rs)).to_numpy()

        @staticmethod
        def MACD(real, fastperiod=12, slowperiod=26, signalperiod=9):
            s = _pd.Series(real)
            dif = s.ewm(span=fastperiod, adjust=False).mean() - s.ewm(span=slowperiod, adjust=False).mean()
            dea = dif.ewm(span=signalperiod, adjust=False).mean()
            hist = dif - dea  # talib 口径:macd - signal(不×2)
            return dif.to_numpy(), dea.to_numpy(), hist.to_numpy()

        @staticmethod
        def BBANDS(real, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0):
            s = _pd.Series(real)
            mid = s.rolling(int(timeperiod)).mean()
            std = s.rolling(int(timeperiod)).std(ddof=0)  # talib 用总体标准差
            upper = mid + nbdevup * std
            lower = mid - nbdevdn * std
            return upper.to_numpy(), mid.to_numpy(), lower.to_numpy()

    tl = _TLShim()
    print("[_talib_compat] ⚠️ talib 未安装,启用 pandas 等价兜底(MA/RSI/MACD/BBANDS)")
