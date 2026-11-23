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
            _dd = ws.get('avg_max_dd_pct', 0) or 0
            out.append((key, {
                'code': code, 'name': name,
                'win_rate': tr_wr, 'avg_ret': tr_ar,
                'max_dd': _dd,
                'best_ret': ws.get('max_win_pct', 0) or 0,
                'worst_ret': ws.get('max_loss_pct', 0) or 0,
                'trigger_count': tr_n,
                'ho_wr': ho_wr, 'ho_ar': ho_ar, 'ho_n': ho_n,
                'score': compute_strategy_score(tr_wr, tr_ar, tr_n,
                                                max_trigger=n_pool, sample_stocks=1,
                                                max_dd=_dd),
            }))
        except Exception:
            continue
    return out


def portfolio_batch(tasks, load_checkpoint, save_checkpoint, *, deadline, cancelled):
    """Reuse only results made with identical bars, variants and scoring inputs."""
    from hashlib import sha256
    from multiprocessing import TimeoutError, get_context
    from time import monotonic
    from pandas.util import hash_pandas_object

    completed, pending = {}, []
    identities = {}
    for task in tasks:
        code, name, frame, variants, split_date, pool_size = task
        definition = [
            "genome-backtest-v1", name, split_date, pool_size, list(frame.columns),
            [(v["id"], v["base_strategy"], v["params"]) for v in variants],
        ]
        identity = sha256(_json.dumps(definition, sort_keys=True, default=str).encode())
        identity.update(hash_pandas_object(frame).to_numpy().tobytes())
        key = f"_genome_backtest:{code}"
        digest = identity.hexdigest()
        cached = load_checkpoint(key) or {}
        if cached.get("digest") == digest and cached.get("rows"):
            completed[code] = cached["rows"]
        else:
            identities[code] = key, digest
            pending.append(task)

    print(f"[daily_backtest] checkpoint_hits={len(completed)} pending={len(pending)}", flush=True)
    if not pending or deadline <= monotonic() or cancelled():
        return [completed[task[0]] for task in tasks if task[0] in completed]

    # Pool.__exit__ terminates active children at the budget boundary. Executor
    # shutdown(wait=True) previously consumed the outer task's remaining budget.
    from os import cpu_count
    workers = min(8, len(pending), max(1, (cpu_count() or 2) - 1))
    with get_context("spawn").Pool(workers) as pool:
        results = pool.imap_unordered(_batch_stock, pending)
        received = 0
        while received < len(pending) and not cancelled():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                code, rows = results.next(timeout=min(1, remaining))
            except TimeoutError:
                continue
            received += 1
            if rows:
                key, digest = identities[code]
                save_checkpoint(key, {"digest": digest, "rows": rows})
                completed[code] = rows
    return [completed[task[0]] for task in tasks if task[0] in completed]


def _batch_stock(task):
    try:
        rows = run_stock(task)
        if len(rows) != len(task[3]):
            print(f"[daily_backtest] {task[0]} variants incomplete: {len(rows)}/{len(task[3])}", flush=True)
            return task[0], []
        return task[0], rows
    except Exception as exc:
        print(f"[daily_backtest] {task[0]} worker error: {type(exc).__name__}", flush=True)
        return task[0], []
