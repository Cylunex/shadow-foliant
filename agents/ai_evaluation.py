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
               ref_price, last_price, realized_pnl_pct, close_reason
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
            'ref_price', 'last_price', 'realized_pnl_pct', 'close_reason']
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


def evaluate_by_source(days: int = 30) -> Dict[str, EvaluationResult]:
    recs = _fetch_period(days)
    by_src: Dict[str, List] = {}
    for r in recs:
        by_src.setdefault(r['source'] or '(unknown)', []).append(r)
    return {src: _evaluate(rs, days, source=src) for src, rs in by_src.items()}


def format_report(result_or_dict) -> str:
    if isinstance(result_or_dict, EvaluationResult):
        results = {result_or_dict.source or 'ALL': result_or_dict}
    else:
        results = result_or_dict
    lines = ['=== AI 推荐评估报告 ===']
    for src, r in results.items():
        lines.append(f'\n[{src}]  样本={r.sample_size} 期间={r.period_days}天')
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


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== AI 评估框架自检 ===')

    overall = evaluate_all(days=30)
    print(format_report(overall))

    print('\n按 source 拆分:')
    by_src = evaluate_by_source(days=30)
    print(format_report(by_src))
