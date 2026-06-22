import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导(子进程 spawn 后重新建立 sys.path)
"""基因组回测的进程池 worker —— 供 task_daily_backtest 跨进程并行。

task_daily_backtest 的"全股池 × 全变体"回测是 N² CPU 密集(30股 × ~120变体 ≈ 3600 次),
单线程实测 45~60 分钟、常踩 deadline。本 worker 把**单只股票跑全部变体**封成一个进程任务:
  - 每只股只 pickle 一次 df(变体共享),进程间传输小;
  - backtest_one 纯在内存 df 上算、无副作用 → 并行结果与串行**逐位一致**(只提速,不改进化质量)。

独立成轻模块(不放 jobs_hub)是为了:子进程 spawn 时只 import 这个 + backtest_engine +
strategy_genome,不必 import 庞大的 jobs_hub,显著降低 7 个 worker 的启动开销。
"""

import json as _json
from typing import Any, List, Tuple


def _agg(ts) -> Tuple[float, float, int]:
    """从 trades 子集算 (win_rate%, avg_ret%, count)。与 task_daily_backtest 内联口径一致。"""
    if not ts:
        return (0.0, 0.0, 0)
    rets = [t.get('ret_pct', 0) or 0 for t in ts]
    wr = sum(1 for x in rets if x > 0) / len(rets) * 100
    return (round(wr, 1), round(sum(rets) / len(rets), 2), len(rets))


def run_stock(task) -> List[Tuple[str, dict]]:
    """单只股票跑全部变体。task=(code, name, df, variants, split_date, n_pool)。
    返回 [(key='sid:vid', result_dict), ...];单变体异常被吞、续下一个。"""
    code, name, df, variants, split_date, n_pool = task
    from backtest_engine import backtest_one
    from strategy_genome import coerce_params, compute_strategy_score
    out: List[Tuple[str, dict]] = []
    for v in variants:
        sid = v['base_strategy']
        vid = v['id']
        params = v['params'] if isinstance(v['params'], dict) else _json.loads(v['params'])
        params = coerce_params(sid, params)
        key = f"{sid}:{vid}"
        hd = int(params.get('hold_days') or 10)
        try:
            r = backtest_one(code, sid, df, None, None, hold_days=hd,
                             stop_pct=8, target_pct=15, params=params)
            if r.get('error'):
                continue
            ws = r.get('summary', {}) or {}
            trades = r.get('trades') or []
            tr_trades = [t for t in trades if (t.get('trigger_date') or '') < split_date]
            ho_trades = [t for t in trades if (t.get('trigger_date') or '') >= split_date]
            tr_wr, tr_ar, tr_n = _agg(tr_trades)
            ho_wr, ho_ar, ho_n = _agg(ho_trades)
            out.append((key, {
                'code': code, 'name': name,
                'win_rate': tr_wr, 'avg_ret': tr_ar,
                'max_dd': ws.get('avg_max_dd_pct', 0) or 0,
                'best_ret': ws.get('max_win_pct', 0) or 0,
                'worst_ret': ws.get('max_loss_pct', 0) or 0,
                'trigger_count': tr_n,
                'ho_wr': ho_wr, 'ho_ar': ho_ar, 'ho_n': ho_n,
                'score': compute_strategy_score(tr_wr, tr_ar, tr_n,
                                                max_trigger=n_pool, sample_stocks=1),
            }))
        except Exception:
            continue
    return out
