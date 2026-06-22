"""规范枚举 / 取值归一 —— 统一项目里大量散落的字符串分类值(标准化 DB 优化配套)。

背景:历史上 rating/trade_type/confidence/status 等列中英混用、来源不一(LLM 出英文、UI 出中文)。
DB 侧对**系统受控列**(job 状态/信号/通知类型/变动类型)用 PG 原生 ENUM;对 **LLM/UI 喂的
rating/confidence** 用"本模块归一到中文规范值 + DB CHECK 约束"(硬枚举会让 LLM 偶发越界值
插入失败而丢数据,故在应用层先归一再入库)。

写入这些列前一律先过对应 normalize_*(),保证落库值在规范集合内。
"""

from __future__ import annotations

# ---------------- 规范集合(中文,用户面向) ----------------
RATINGS = ('强烈买入', '买入', '持有', '卖出', '强烈卖出')
CONFIDENCES = ('高', '中', '低')

# ---------------- 系统受控值(英文,内部代码) ----------------
JOB_STATUSES = ('success', 'error', 'skipped', 'running')
SIGNALS = ('BUY', 'SELL', 'HOLD')
NOTIF_TYPES = ('entry', 'take_profit', 'stop_loss')          # 监测告警类型
CHANGE_TYPES = ('买入', '卖出', '红利入账', '新增', '调整', '删除')  # trade_records 合并表的变动类型

# ---------------- 归一映射 ----------------
_RATING_MAP = {
    'strong_buy': '强烈买入', 'strong buy': '强烈买入', 'strongbuy': '强烈买入',
    'buy': '买入', '增持': '买入', '推荐': '买入',
    'hold': '持有', 'neutral': '持有', '中性': '持有', '观望': '持有',
    'candidate': '持有',  # unified_selection 选股候选(只追踪非操作建议)显式归"持有",免走 default 静默改写
    'sell': '卖出', '减持': '卖出', 'reduce': '卖出',
    'strong_sell': '强烈卖出', 'strong sell': '强烈卖出',
}
_CONFIDENCE_MAP = {
    'high': '高', 'h': '高', '高': '高',
    'medium': '中', 'mid': '中', 'm': '中', '中': '中', '中等': '中',
    'low': '低', 'l': '低', '低': '低',
}
_TRADE_TYPE_MAP = {
    'buy': '买入', '买入': '买入', '申购': '买入', '定投': '买入',
    'sell': '卖出', '卖出': '卖出', '赎回': '卖出',
    '红利入账': '红利入账', 'dividend': '红利入账', '分红': '红利入账',
    'add': '新增', '新增': '新增',
    'update': '调整', '调整': '调整',
    'delete': '删除', '删除': '删除',
}


def normalize_rating(value, default: str = '持有') -> str:
    """任意 rating 文本 → 中文规范值。含中文'买/卖/持'子串也能识别。未知归 default。"""
    if not value:
        return default
    s = str(value).strip()
    low = s.lower()
    if low in _RATING_MAP:
        return _RATING_MAP[low]
    if s in RATINGS:
        return s
    # 中文子串兜底
    if '强烈买' in s or '强买' in s:
        return '强烈买入'
    if '强烈卖' in s or '强卖' in s:
        return '强烈卖出'
    if '买' in s:
        return '买入'
    if '卖' in s:
        return '卖出'
    if '持有' in s or '观望' in s or '中性' in s:
        return '持有'
    return default


def normalize_confidence(value, default: str = '中') -> str:
    if not value:
        return default
    s = str(value).strip()
    return _CONFIDENCE_MAP.get(s.lower(), s if s in CONFIDENCES else default)


def normalize_trade_type(value, default: str = '调整') -> str:
    if not value:
        return default
    s = str(value).strip()
    return _TRADE_TYPE_MAP.get(s.lower(), s if s in CHANGE_TYPES else default)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    for v in ['buy', 'strong_buy', 'hold', '持有', '买入', '增持', 'SELL', '看多', None]:
        print(repr(v), '->', normalize_rating(v))
    for v in ['high', '中', 'LOW', 'x']:
        print(repr(v), '->', normalize_confidence(v))
    for v in ['sell', 'delete', '红利入账', 'update']:
        print(repr(v), '->', normalize_trade_type(v))
