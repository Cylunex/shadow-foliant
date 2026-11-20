from time import monotonic

import pandas as pd

from analysis.genome_bt_worker import portfolio_batch


def test_batch_reuses_only_results_with_matching_frozen_inputs():
    bars = pd.DataFrame(
        {"Open": [10.0, 11.0], "High": [11.0, 12.0], "Low": [9.0, 10.0],
         "Close": [10.5, 11.5], "Volume": [1000, 1100]},
        index=pd.date_range("2026-11-17", periods=2),
    )
    variants = [{"id": 1, "base_strategy": "unknown_fixture", "params": {}}]
    tasks = [(code, "示例", bars, variants, "2026-11-17", 2)
             for code in ("600001", "600002")]
    saved = {}
    run = lambda tasks, seconds: portfolio_batch(
        tasks, saved.get, saved.__setitem__, deadline=monotonic() + seconds,
        cancelled=lambda: False,
    )
    first = run(tasks[:1], 15)
    assert len(first) == 1 and len(saved) == 1
    assert run(tasks, -1) == first
    changed = bars.assign(Close=[10.0, 11.0])
    assert run([("600001", "示例", changed, variants, "2026-11-17", 2)], -1) == []
    assert len(run(tasks, 15)) == len(saved) == 2
