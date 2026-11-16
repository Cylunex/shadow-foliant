import json

import pandas as pd

import _bootstrap  # noqa: F401
from data.research_store import ResearchStore


class BoundedCursor:
    def __init__(self, rows, columns):
        self.rows = iter(rows)
        self.description = [(name,) for name in columns]
        self.sizes = []

    def fetchmany(self, size):
        from itertools import islice
        self.sizes.append(size)
        return list(islice(self.rows, size))

    def fetchall(self):
        raise AssertionError('market panels must not fetch all rows')


def test_market_panel_batches_preserve_values_and_bound_repeated_labels():
    columns = ['symbol', 'trade_date', 'close', 'volume', 'provider',
               'quality_status', 'dataset_id']
    rows = [(f'{index % 37:06}', f'2026-10-{index % 28 + 1:02}',
             13.123456789 + index, 100000.25, 'synthetic-provider', 'ok',
             f'dataset-{index % 28}') for index in range(9000)]
    cursor = BoundedCursor(rows, columns)
    compact = ResearchStore._market_frame(cursor)
    original = pd.DataFrame(rows, columns=columns)
    pd.testing.assert_frame_equal(
        compact.astype(original.dtypes.to_dict()), original, check_exact=True)
    assert len(cursor.sizes) == 4 and set(cursor.sizes) == {4096}
    assert compact.memory_usage(deep=True).sum() < original.memory_usage(deep=True).sum() / 3
    assert compact['trade_date'].max() == original['trade_date'].max()
    assert compact['close'].dtype == original['close'].dtype


def test_manifest_panel_preserves_later_revision_order_across_batches():
    rows = [('600001', '2026-10-20', 'synthetic', 'ok', 'batch',
             json.dumps({'close': index + 0.123456789, 'volume': 100000.25}))
            for index in range(4100)]
    cursor = BoundedCursor(rows, ['symbol', 'trade_date', 'provider',
                                  'quality_status', 'dataset_id', 'payload'])
    compact = ResearchStore._market_frame(cursor, observations=True)
    result = compact.drop_duplicates(['symbol', 'trade_date'], keep='last')
    assert len(result) == 1 and result.iloc[0]['close'] == 4099.123456789
    assert result.iloc[0]['volume'] == 100000.25


def test_dataset_retry_releases_previous_attempt_frames(monkeypatch):
    import weakref
    import numpy as np
    from analysis.local_stock_selector import LocalStockSelector, _DatasetPublicationChanged

    selector = object.__new__(LocalStockSelector)
    previous = []
    reference = ({'symbol': '600001'} for _ in range(1))
    def attempt(*args, **kwargs):
        assert kwargs['wencai_reference'] == [{'symbol': '600001'}]
        if not previous:
            panel = np.zeros(1024)
            previous.append(weakref.ref(panel))
            raise _DatasetPublicationChanged
        assert previous[0]() is None
        return {'status': 'success'}
    monkeypatch.setattr(selector, '_run_once', attempt)
    assert selector.run('2026-11-16', persist=False, wencai_reference=reference) == {
        'status': 'success'}


def test_batched_independent_hash_matches_legacy_identity_with_duplicate_indexes():
    import numpy as np
    from analysis.independent_selector import _frame_hash
    frame = pd.DataFrame({
        'symbol': ['600002', '600001'] * 2050,
        'trade_date': ['2026-10-02', '2026-10-01'] * 2050,
        'close': np.arange(4100, dtype=float) + 0.123456789,
        'provider': ['fixture'] * 4100,
    })
    frame.index = [1] * len(frame)
    assert _frame_hash(frame, frame.columns) == (
        'ccf9c3c750a282a3011e1922467054944e67fcddf90e9197fc967cde8756b96e')
