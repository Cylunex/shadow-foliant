"""BaoStock(证券宝 www.baostock.com)K线源封装 —— 免费、开源、无需注册/token,**全历史(1990 至今)**。

定位:datahub kline 的**长历史源 + 独立兜底源**。最大价值=解「回测深度受限(新浪单次~365根)」——
长周期(2y/3y/5y)优先走 baostock 拿全历史;短周期作末位兜底(腾讯/新浪/东财全挂时的独立免费源)。

⚠️ 三条硬约束(本模块已处理):
  (a) **须 bs.login() 后才能查** —— 惰性登录、进程内复用、会话失效自动重登;
  (b) **不可并发连接访问** —— 全局锁串行化所有 baostock 调用(违反会进黑名单);
  (c) 每日 ≤5 万次请求 —— 故仅作长历史/兜底,不做高频批量主源。

可选依赖:未 `pip install baostock` 则本源静默不可用(kline 返回空 DF,上层自动跳到下一源),像 mootdx。
输出严格对齐 datahub 既有 K线格式:DatetimeIndex(name='Date') + 大写列 Open/Close/High/Low/Volume,
volume 单位「股」(实测 baostock 即股,与新浪/东财×100 后同口径)。
"""
import threading
import time as _time
from datetime import datetime, timedelta

_LOCK = threading.Lock()          # 串行化所有 baostock 调用(不可并发连接)
_BS = None                        # baostock 模块(惰性 import)
_LOGGED_IN = False

# ⚠️ 2026-06-30 防 baostock 雪崩:连续失败计数 + 冷却。
# 根因:baostock 用底层 socket(无 socket-level timeout),服务端慢响应时孤儿线程持 _LOCK 卡 OS RTO 120s,
# 后续 kline 调用 with _LOCK 等不到锁、又重 login 又卡 → 单源 100s+ 拖累 _route 一直 cooldown 不到。
# 修法:
#  ① 等锁最多 2s,拿不到立即返空让 _route 跳源(不持锁等 100s+)
#  ② 自带"连续失败 2 次 → 冷却 5 分钟"短路,期间秒返空,baostock 服务恢复后自动重试
_FAIL_COUNT = 0
_LAST_FAIL = 0.0
_COOLDOWN_FAILS = 2
_COOLDOWN_SEC = 300       # 5 分钟


def _in_cooldown() -> bool:
    return _FAIL_COUNT >= _COOLDOWN_FAILS and (_time.time() - _LAST_FAIL) < _COOLDOWN_SEC


def _mark_fail():
    global _FAIL_COUNT, _LAST_FAIL, _LOGGED_IN
    _FAIL_COUNT += 1
    _LAST_FAIL = _time.time()
    _LOGGED_IN = False    # 失败一律重置登录,下次重连


def _mark_ok():
    global _FAIL_COUNT
    _FAIL_COUNT = 0


def breaker_open() -> bool:
    """对外查询冷却状态(供 _notify_data_unavailable 显示具体卡死的源)。"""
    return _in_cooldown()

# 与 datahub._PERIOD_DAYS 对齐(自然日)
_PERIOD_DAYS = {"1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730, "3y": 1095, "5y": 1825}


def available() -> bool:
    """baostock 是否可 import(未装则本源不可用)。"""
    try:
        import baostock  # noqa: F401
        return True
    except Exception:
        return False


def _ensure():
    """惰性 import + 登录(进程内复用)。失败抛异常,上层 except 跳过本源。"""
    global _BS, _LOGGED_IN
    if _BS is None:
        import baostock as _bs
        _BS = _bs
    if not _LOGGED_IN:
        lg = _BS.login()
        if getattr(lg, 'error_code', '1') != '0':
            raise RuntimeError(f'baostock login 失败: {getattr(lg, "error_msg", "?")}')
        _LOGGED_IN = True
    return _BS


def _bs_code(code: str) -> str:
    """6 位代码 → baostock 代码(sh.xxxxxx / sz.xxxxxx)。
    沪(sh):6 开头股票、5 开头基金、900 沪B、688 科创;深/京(sz):00/30、15/16、8x/920 北交所、其余。"""
    c = ''.join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
    if c[:1] in ('6', '5') or c[:3] == '900' or c[:3] == '688':
        return f'sh.{c}'
    return f'sz.{c}'


def index_kline(code: str, period: str = "3y", interval: str = "1d"):
    """指数日线(沪深300/中证500 等)。baostock 支持指数历史,但指数代码规则与个股不同:
    000xxx 中证/上证系列 → sh.、399xxx 深证系列 → sz.(个股规则 _bs_code 会把 000300 误判成
    sz.000300 取空)。供 datahub.index_kline 域调用,勿走个股 kline 的代码推断。"""
    c = ''.join(ch for ch in str(code) if ch.isdigit())[-6:].zfill(6)
    bs = ('sz.' if c.startswith('399') else 'sh.') + c
    return kline(code, period, interval, 'raw', bs_code=bs)


def kline(code: str, period: str = "1y", interval: str = "1d", adjust: str = "raw",
          bs_code: str = None):
    """返回 datahub 同款 K线 DataFrame(DatetimeIndex='Date' + 大写 OCHLV)或空 DF。

    adjust='qfq'→前复权(adjustflag=2,真·非东财 qfq 源)/'raw'→不复权(adjustflag=3)。仅日线。
    bs_code: 显式指定 baostock 代码(如指数 'sh.000300'),不传按个股规则 _bs_code 推断。
    任何异常/未装/登录失败 → 返回空 DF(纯兜底,绝不抛)。"""
    try:
        import pandas as pd
    except Exception:
        return None
    if interval not in ('1d', 'daily', '101'):
        return pd.DataFrame()                  # 仅日线,其余交回主链
    # ⭐ 冷却期:连续失败 2 次 → 5 分钟内秒拒,让 _route 跳源(不再卡 100s+ OS RTO)
    if _in_cooldown():
        return pd.DataFrame()
    days = _PERIOD_DAYS.get(period, 365)
    start = (datetime.now() - timedelta(days=int(days) + 10)).strftime('%Y-%m-%d')  # +10 冗余
    end = datetime.now().strftime('%Y-%m-%d')
    bscode = bs_code or _bs_code(code)
    adjustflag = '2' if str(adjust) == 'qfq' else '3'   # 2=前复权 3=不复权
    rows = []
    # ⭐ 等锁最多 2s,拿不到立即返空(防孤儿持锁拖累后续调用)。锁里 login/query 卡到 OS RTO 是它的事,
    # 但本调用 2s 内一定脱身让 _route 跳源,且累计失败触发冷却 → 整链最多卡 1 次孤儿,不再雪崩。
    if not _LOCK.acquire(timeout=2):
        return pd.DataFrame()
    try:
        try:
            bs = _ensure()
            rs = bs.query_history_k_data_plus(
                bscode, "date,open,high,low,close,volume",
                start_date=start, end_date=end, frequency='d', adjustflag=adjustflag)
            if getattr(rs, 'error_code', '1') != '0':
                _mark_fail()
                return pd.DataFrame()
            while rs.next():
                rows.append(rs.get_row_data())
        except Exception:
            _mark_fail()
            return pd.DataFrame()
    finally:
        _LOCK.release()
    if not rows:
        # 查询成功但 0 行 =「该代码无数据」(北交所/退市/指数误入个股规则),不是源故障
        # —— 不计熔断、不重置登录(2026-07-17 修:原来一只北交所码 raw+qfq 各查一次就把
        # baostock 整源打进 5 分钟冷却,期间所有 2y/3y 长历史静默退化 ~365 根;
        # 口径与 pywencai_safe「返回空 df ≠ 失败」一致)。
        return pd.DataFrame()
    try:
        df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
        for c in ('open', 'high', 'low', 'close', 'volume'):
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df.dropna(subset=['close'])
        df = df[df['close'] > 0]
        if df.empty:
            return pd.DataFrame()
        df['Date'] = pd.to_datetime(df['date'], errors='coerce')
        df = df.dropna(subset=['Date']).set_index('Date')
        # 严格对齐 datahub:大写 Open/Close/High/Low/Volume(volume 已是「股」)
        out = df[['open', 'close', 'high', 'low', 'volume']].copy()
        out.columns = ['Open', 'Close', 'High', 'Low', 'Volume']
        _mark_ok()   # 成功重置失败计数 → 解除冷却
        return out
    except Exception:
        return pd.DataFrame()
