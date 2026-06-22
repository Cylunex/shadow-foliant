import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""AI 推荐评估框架 — 借鉴 FinceptTerminal 的 EvaluationResult 设计

回答的核心问题：**AI 的推荐到底准不准？**

对 ai_recommendations 表过去 N 天的推荐做"事后评估"——**以真实盈亏为口径**:
  - win_rate_pct       真实胜率 = 收益>0 占比(含持有中的浮动,不只看触发)
  - avg_return_pct     平均真实收益(已了结用 realized_pnl_pct;pending 用 last vs ref 浮动)
  - median/max_loss    中位收益 / 最大亏损
  - profit_factor      盈亏比 = 总盈利 / 总亏损
  - closed / pending   已了结(止盈/止损/过期) / 持有中

口径要点:① 不再用"触发率"这种虚指标 ② pending 计入分母(消除幸存者偏差)
  ③ 超期(monitor 的 PENDING_EXPIRE_DAYS)未触发的按浮动盈亏了结。
按 source 维度分组评估（凌晨综合策略 / 龙虎榜 / 板块 ... 哪个 source 真能赚）。

接口：
  evaluate_all(days=30)
  evaluate_by_source(days=30)  -> {source: EvaluationResult}
  format_report(result_or_dict) -> str
"""

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from db_compat import connect as db_connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')


# source / status 字段的中文映射 — 给推送报告显示用(未匹配的原样返回)
SOURCE_CN = {
    'overnight_strategy':     '凌晨综合策略',
    'wf_daily_strategy_scan': '盘后策略扫描',
    'wf_overnight_to_rec':    '隔夜入池',
    'wf_selection_to_rec':    '选股入池',
    'unified_selection':      '综合选股',
    'main_force':             '主力资金',
    'low_price_bull':         '低价擒牛',
    'small_cap':              '小市值',
    'profit_growth':          '净利增长',
    'value':                  '低估值',
    '(unknown)':              '未知',
    '':                       '未知',
}
STATUS_CN = {
    'pending':  '持有中',
    'target':   '止盈',
    'stop':     '止损',
    'expired':  '到期',
}


def _src_cn(s: str) -> str:
    return SOURCE_CN.get(s or '', s or '未知')


def _st_cn(s: str) -> str:
    return STATUS_CN.get(s or '', s or '持有中')


@dataclass
class EvaluationResult:
    score: float = 0.0
    grade: str = 'N/A'
    sample_size: int = 0
    period_days: int = 0
    source: str = ''
    metrics: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _grade(score: float) -> str:
    if score >= 80: return 'A (优秀)'
    if score >= 65: return 'B (良好)'
    if score >= 50: return 'C (及格)'
    if score >= 35: return 'D (偏弱)'
    return 'F (差)'


def _fetch_period(days: int, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """拉指定时间区间的推荐(含真实盈亏追踪列)"""
    try:  # 确保真实盈亏列已存在(老库幂等补齐)
        from ai_recommendation_monitor import _ensure_perf_columns
        _ensure_perf_columns()
    except Exception:
        pass
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    sql = '''
        SELECT id, symbol, name, source, target_price, take_profit, stop_loss,
               hit_target_at, hit_stop_at, recommended_at,
               ref_price, last_price, realized_pnl_pct, close_reason,
               confidence, rating
        FROM ai_recommendations
        WHERE recommended_at >= ?
    '''
    params: List[Any] = [cutoff]
    if source:
        sql += ' AND source = ?'
        params.append(source)
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()
    keys = ['id', 'symbol', 'name', 'source', 'target_price', 'take_profit',
            'stop_loss', 'hit_target_at', 'hit_stop_at', 'recommended_at',
            'ref_price', 'last_price', 'realized_pnl_pct', 'close_reason',
            'confidence', 'rating']
    return [dict(zip(keys, r)) for r in rows]


def _rec_return(r: Dict[str, Any]) -> Optional[float]:
    """单条推荐的真实收益%:已了结用 realized_pnl_pct;pending 用浮动(last vs ref);无数据返回 None。"""
    if r.get('realized_pnl_pct') is not None:
        return float(r['realized_pnl_pct'])
    ref, last = r.get('ref_price'), r.get('last_price')
    if ref and last:
        return round((float(last) - float(ref)) / float(ref) * 100, 2)
    return None


def _evaluate(recs: List[Dict[str, Any]], days: int, source: str = '') -> EvaluationResult:
    """对一批推荐计算指标"""
    n = len(recs)
    if n == 0:
        return EvaluationResult(period_days=days, source=source, sample_size=0,
                                metrics={'note': 'no_samples'})

    hit_target = sum(1 for r in recs if r['hit_target_at'])
    hit_stop = sum(1 for r in recs if r['hit_stop_at'])
    closed = sum(1 for r in recs if r.get('close_reason'))
    pending = n - closed

    # —— 真实收益口径(含 pending 浮动,消除幸存者偏差) ——
    rets = [x for x in (_rec_return(r) for r in recs) if x is not None]
    n_ret = len(rets)
    no_price = n - n_ret  # 既无 ref_price 又无 last_price 的老数据,无法计真实收益
    if n_ret:
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x <= 0]
        win_rate = len(wins) / n_ret * 100
        avg_ret = sum(rets) / n_ret
        srt = sorted(rets)
        median_ret = srt[n_ret // 2] if n_ret % 2 else (srt[n_ret // 2 - 1] + srt[n_ret // 2]) / 2
        max_loss = min(rets)
        gain_sum, loss_sum = sum(wins), abs(sum(losses))
        profit_factor = round(gain_sum / loss_sum, 2) if loss_sum > 0 else (999.0 if gain_sum > 0 else 0.0)
    else:
        win_rate = avg_ret = median_ret = max_loss = 0.0
        profit_factor = 0.0

    # 综合得分锚定真实盈亏:60% 由平均收益映射([-20%,+20%]→[0,100]) + 40% 胜率
    ret_score = max(0.0, min(100.0, 50 + avg_ret * 2.5))
    score = round(ret_score * 0.6 + win_rate * 0.4, 1) if n_ret else 0.0

    return EvaluationResult(
        score=score,
        grade=_grade(score),
        sample_size=n,
        period_days=days,
        source=source,
        metrics={
            'win_rate_pct': round(win_rate, 1),         # 真实胜率(收益>0 占比,含 pending)
            'avg_return_pct': round(avg_ret, 2),        # 平均真实收益
            'median_return_pct': round(median_ret, 2),
            'max_loss_pct': round(max_loss, 2),
            'profit_factor': profit_factor,             # 盈亏比(总盈利/总亏损)
            'n_with_return': n_ret,
            'no_price_data': no_price,
            'closed': closed, 'pending': pending,
            'hit_target': hit_target, 'hit_stop': hit_stop,
        },
    )


def _parse_dt(val) -> datetime:
    if isinstance(val, datetime):
        return val
    s = str(val).replace('T', ' ').replace('+00:00', '')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(s[:26])


def evaluate_all(days: int = 30) -> EvaluationResult:
    return _evaluate(_fetch_period(days), days, source='ALL')


# ── 维度分桶(借鉴 DecisionSignal 的"按维度冻结统计胜率"思路)─────────────
# 回答:不只"整体胜率多少",而是"哪个来源/哪种信心度/哪种持有周期真正赚钱"→ 回喂选股门槛。
VALID_DIMENSIONS = ['source', 'confidence', 'horizon', 'outcome', 'month']

CONFIDENCE_CN = {'高': '高信心', '中': '中信心', '低': '低信心', '': '未标信心'}
# 桶排序权重(越小越靠前);未列出的桶排末尾按样本量
_BUCKET_ORDER = {
    'confidence': {'高': 0, '中': 1, '低': 2, '': 9},
    'horizon': {'短线(≤3日)': 0, '波段(4-10日)': 1, '中长线(>10日)': 2, '持有中': 8, '无数据': 9},
    'outcome': {'止盈': 0, '止损': 3, '到期': 2, '持有中': 1},
}


def _holding_days(r: Dict[str, Any]) -> Optional[int]:
    """推荐到了结(或到现在)的持有天数;无入场时间返回 None。"""
    rec_at = r.get('recommended_at')
    if not rec_at:
        return None
    try:
        start = _parse_dt(rec_at)
    except Exception:
        return None
    end_raw = r.get('hit_target_at') or r.get('hit_stop_at')
    try:
        end = _parse_dt(end_raw) if end_raw else datetime.now()
    except Exception:
        end = datetime.now()
    return max(0, (end - start).days)


def _bucket_key(r: Dict[str, Any], dim: str) -> str:
    """把一条推荐归到指定维度的桶。返回桶的原始键(展示名在 _bucket_label 里转)。"""
    if dim == 'source':
        return r.get('source') or '(unknown)'
    if dim == 'confidence':
        return str(r.get('confidence') or '')
    if dim == 'outcome':
        return r.get('close_reason') or 'pending'
    if dim == 'month':
        return str(r.get('recommended_at') or '')[:7] or '(unknown)'
    if dim == 'horizon':
        cr = r.get('close_reason')
        if not cr:
            return '持有中'
        d = _holding_days(r)
        if d is None:
            return '无数据'
        if d <= 3:
            return '短线(≤3日)'
        if d <= 10:
            return '波段(4-10日)'
        return '中长线(>10日)'
    return '(unknown)'


def _bucket_label(key: str, dim: str) -> str:
    if dim == 'source':
        return _src_cn(key)
    if dim == 'confidence':
        return CONFIDENCE_CN.get(key, key or '未标信心')
    if dim == 'outcome':
        return _st_cn(key)
    return key or '(unknown)'


def evaluate_by(dim: str = 'source', days: int = 30) -> Dict[str, EvaluationResult]:
    """按维度分桶评估。dim ∈ VALID_DIMENSIONS。返回 {桶键: EvaluationResult}(已按维度排序)。"""
    if dim not in VALID_DIMENSIONS:
        raise ValueError(f'不支持的维度 {dim};可用:{VALID_DIMENSIONS}')
    recs = _fetch_period(days)
    by_bucket: Dict[str, List] = {}
    for r in recs:
        by_bucket.setdefault(_bucket_key(r, dim), []).append(r)
    results = {k: _evaluate(rs, days, source=k) for k, rs in by_bucket.items()}
    order = _BUCKET_ORDER.get(dim)
    if order is not None:
        keys = sorted(results, key=lambda k: (order.get(k, 5), -results[k].sample_size))
    else:  # source/month:source 按样本量,month 按时间
        keys = (sorted(results) if dim == 'month'
                else sorted(results, key=lambda k: -results[k].sample_size))
    return {k: results[k] for k in keys}


def evaluate_by_source(days: int = 30) -> Dict[str, EvaluationResult]:
    """按来源分桶(evaluate_by('source') 的兼容别名)。"""
    return evaluate_by('source', days)


_DIM_CN = {'source': '来源', 'confidence': '信心度', 'horizon': '持有周期',
           'outcome': '了结方式', 'month': '推荐月份'}


def format_buckets(results: Dict[str, EvaluationResult], dim: str = 'source') -> str:
    """把分桶评估结果格式化成文本块(桶标签按维度翻译)。"""
    lines = [f'=== AI 推荐评估 · 按{_DIM_CN.get(dim, dim)}分桶 ===']
    for key, r in results.items():
        label = _bucket_label(key, dim)
        lines.append(f'\n[{label}]  样本={r.sample_size} 期间={r.period_days}天')
        if r.sample_size == 0:
            lines.append('  (无样本)')
            continue
        m = r.metrics
        if not m.get('n_with_return'):
            lines.append('  (样本无入场价,无法计真实收益)')
            continue
        lines.append(f"  综合得分: {r.score}  等级: {r.grade}")
        lines.append(f"  真实胜率: {m['win_rate_pct']}%  平均收益: {m['avg_return_pct']:+}%  "
                     f"盈亏比: {m['profit_factor']}  (已了结{m['closed']}/持有中{m['pending']})")
    return '\n'.join(lines)


def format_report(result_or_dict) -> str:
    if isinstance(result_or_dict, EvaluationResult):
        results = {result_or_dict.source or 'ALL': result_or_dict}
    else:
        results = result_or_dict
    lines = ['=== AI 推荐评估报告 ===']
    for src, r in results.items():
        lines.append(f'\n[{_src_cn(src)}]  样本={r.sample_size} 期间={r.period_days}天')
        if r.sample_size == 0:
            lines.append('  (无样本)')
            continue
        lines.append(f'  综合得分: {r.score}  等级: {r.grade}')
        m = r.metrics
        lines.append(f"  真实胜率: {m['win_rate_pct']}%  平均收益: {m['avg_return_pct']:+}%  "
                     f"中位: {m['median_return_pct']:+}%  最大亏损: {m['max_loss_pct']}%")
        lines.append(f"  盈亏比: {m['profit_factor']}  已了结: {m['closed']}  "
                     f"持有中: {m['pending']}  (止盈{m['hit_target']}/止损{m['hit_stop']})")
        if m.get('no_price_data'):
            lines.append(f"  ⚠️ {m['no_price_data']} 条老数据无入场价,未计入收益")
    return '\n'.join(lines)


def format_unowned_picks(held_codes: set, days: int = 30,
                         top_winners: int = 10, top_losers: int = 5) -> str:
    """列出"推荐了但用户没买"的票, 按真实收益排序 — 让用户看到错过的机会 + 幸亏没买的雷。

    Args:
        held_codes: 用户当前持仓代码集合(6 位 zfill)。空集合表示完全没持仓 → 列出所有。
        days: 评估区间(天)
        top_winners: 涨幅 Top N(错过的机会)
        top_losers: 跌幅 Top N(幸亏没买)

    Returns:
        Markdown 文本块, 无未持仓样本返回空字符串。
    """
    recs = _fetch_period(days)
    unowned = []
    for r in recs:
        sym = str(r.get('symbol') or '').zfill(6)
        if not sym or sym in held_codes:
            continue
        ret = _rec_return(r)
        if ret is None:
            continue
        unowned.append({
            'symbol': sym,
            'name': r.get('name', '') or '',
            'source': r.get('source', '') or '(unknown)',
            'status': r.get('close_reason') or 'pending',
            'return_pct': ret,
        })
    if not unowned:
        return ''
    unowned.sort(key=lambda x: -x['return_pct'])

    lines = ['', '=== 推荐但未持仓(按真实收益排序) ===']
    n_w = min(top_winners, len(unowned))
    if n_w > 0 and unowned[0]['return_pct'] > 0:
        lines.append(f'\n📈 错过的机会 Top {n_w}:')
        for r in unowned[:n_w]:
            if r['return_pct'] <= 0:
                break
            lines.append(f"  {r['symbol']}  {r['name'][:8]:<8s}  "
                         f"{_src_cn(r['source']):<12s}  {_st_cn(r['status']):<6s}  "
                         f"{r['return_pct']:+6.2f}%")
    losers = [x for x in unowned if x['return_pct'] < 0]
    if losers:
        losers.sort(key=lambda x: x['return_pct'])  # 跌幅最大在前
        n_l = min(top_losers, len(losers))
        lines.append(f'\n📉 幸亏没买 Top {n_l} (跌幅最大):')
        for r in losers[:n_l]:
            lines.append(f"  {r['symbol']}  {r['name'][:8]:<8s}  "
                         f"{_src_cn(r['source']):<12s}  {_st_cn(r['status']):<6s}  "
                         f"{r['return_pct']:+6.2f}%")
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== AI 评估框架自检 ===')

    overall = evaluate_all(days=30)
    print(format_report(overall))

    print('\n按 source 拆分:')
    by_src = evaluate_by_source(days=30)
    print(format_report(by_src))

    # 自检:未持仓明细(用空集合 = 全部都算"未持仓")
    print('\n未持仓推荐明细(测试用空持仓集合):')
    print(format_unowned_picks(held_codes=set(), days=30))
