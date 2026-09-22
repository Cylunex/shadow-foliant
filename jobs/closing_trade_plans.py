"""Rebuild next-session rule plans from warmed local qfq bars after the close."""
from __future__ import annotations

from datetime import datetime
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from application.results import clean_json, payload_hash
from jobs.intraday_decision_monitor import build_monitor_pool

SNAPSHOT_KEY = 'closing_trade_plans'
SHANGHAI = ZoneInfo('Asia/Shanghai')


def _cached_bars(symbol):
    import datahub
    # cache_only must remain explicit: a missing bar never starts provider I/O.
    return datahub.kline(symbol, '1y', '1d', adjust='qfq', cache_only=True)


def build_closing_plans(*, formal, holdings, now=None, bar_loader=None,
                        plan_builder=None, budget_seconds=120, monotonic=time.monotonic):
    now = now or datetime.now(SHANGHAI)
    now = now.replace(tzinfo=SHANGHAI) if now.tzinfo is None else now.astimezone(SHANGHAI)
    day = now.date().isoformat()
    if (now.hour, now.minute) < (15, 0):
        return {'status': 'skipped', 'reason': 'before_market_close', 'plans': {}}
    if not formal.get('run_id') or str(formal.get('selection_date') or '')[:10] != day:
        return {'status': 'skipped', 'reason': 'today_formal_selection_unavailable', 'plans': {}}
    if plan_builder is None:
        from analysis.trade_plan import build_trade_plan
        plan_builder = build_trade_plan
    bar_loader = bar_loader or _cached_bars
    pool = build_monitor_pool(formal, holdings)
    deadline = monotonic() + max(0, min(120, budget_seconds))
    plans = {}
    for item in pool[:115]:
        symbol = item['symbol']
        basis = {'plan_as_of': None, 'plan_generated_at': now.isoformat(timespec='seconds'),
                 'price_basis': 'post_close_cached_qfq', 'available': False}
        try:
            if monotonic() >= deadline:
                raise ValueError('closing_plan_budget_exhausted')
            frame = bar_loader(symbol)
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                raise ValueError('closing_daily_bars_missing')
            frame = frame.copy()
            frame.columns = [str(column).title() for column in frame.columns]
            if not isinstance(frame.index, pd.DatetimeIndex):
                raise ValueError('closing_daily_bar_dates_invalid')
            if frame.index.tz is not None:
                frame.index = frame.index.tz_convert(SHANGHAI).tz_localize(None)
            frame = frame.sort_index()
            if frame.index.hasnans or frame.index.normalize().has_duplicates:
                raise ValueError('closing_daily_bar_dates_invalid')
            basis['plan_as_of'] = frame.index[-1].date().isoformat()
            if basis['plan_as_of'] != day:
                raise ValueError('closing_daily_bars_not_current')
            try:
                cached_at = datetime.fromtimestamp(frame.attrs['datahub_cache_written_at'], SHANGHAI)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise ValueError('closing_daily_cache_time_missing') from None
            basis['input_cached_at'] = cached_at.isoformat(timespec='seconds')
            if not now.replace(hour=15, minute=0, second=0, microsecond=0) <= cached_at <= now:
                raise ValueError('closing_daily_cache_not_post_close')
            frame = frame.tail(250)
            columns = ['Open', 'High', 'Low', 'Close', 'Volume']
            if len(frame) < 70 or not all(column in frame for column in columns):
                raise ValueError('closing_daily_history_incomplete')
            numbers = frame[columns].apply(pd.to_numeric, errors='coerce')
            if (not np.isfinite(numbers.to_numpy()).all()
                    or (numbers[['Open', 'High', 'Low', 'Close']] <= 0).any().any()
                    or (numbers['Volume'] < 0).any()
                    or (numbers['High'] < numbers['Low']).any()):
                raise ValueError('closing_daily_bar_values_invalid')
            if numbers['Volume'].iloc[-1] <= 0:
                raise ValueError('closing_daily_bar_not_traded')
            frame[columns] = numbers
            plan = plan_builder(symbol, frame, name=item.get('name') or '', market_signal={
                'action': 'unknown', 'reason': '次日开盘前需重新确认市场总闸与行情',
            })
            plans[symbol] = clean_json({**plan, **basis, 'available': plan.get('available') is True,
                'input_hash': payload_hash({'dates': frame.index.astype(str).tolist(),
                                            'ohlcv': numbers.to_dict(orient='list')}),
                'input_rows': len(frame), 'preview_only': True, 'auto_execution': False})
        except Exception as exc:
            code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith('closing_') \
                else 'closing_plan_build_failed'
            plans[symbol] = {**basis, 'blockers': [code], 'preview_only': True,
                             'auto_execution': False}
    available = sum(plan.get('available') is True for plan in plans.values())
    return {'schema_version': 'closing-trade-plans-v1',
            'status': 'complete' if pool and available == len(pool) else 'degraded',
            'trade_date': day, 'selection_run_id': formal['run_id'],
            'generated_at': now.isoformat(timespec='seconds'),
            'requested_count': len(pool), 'available_count': available,
            'omitted_symbols': [item['symbol'] for item in pool[115:]],
            'plans': plans, 'preview_only': True, 'auto_execution': False}


def refresh_closing_plans():
    from data.research_store import ResearchStore
    from portfolio_db import portfolio_db
    from jobs.jobs_hub import save_indicator_snapshot

    result = build_closing_plans(
        formal=ResearchStore(ensure_schema=False).latest_formal_selection() or {},
        holdings=portfolio_db.get_all_stocks() or [],
    )
    if result.get('status') != 'skipped':
        save_indicator_snapshot(SNAPSHOT_KEY, result)
    return result


def latest_snapshot():
    from jobs.jobs_hub import get_indicator_snapshot
    return get_indicator_snapshot(SNAPSHOT_KEY) or {}
