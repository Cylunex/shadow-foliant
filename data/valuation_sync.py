"""Resumable, single-writer, same-day valuation enrichment with bounded fallbacks."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import logging
import os
from pathlib import Path
import time

import pandas as pd

from data.sources import baostock, valuation as sources
from data.valuation_contract import TZ, complete, iso_day, merge_snapshot, number

log = logging.getLogger(__name__)


def _setting(name, default, maximum):
    try:
        return max(0, min(maximum, int(os.getenv(name, str(default)))))
    except (ValueError, TypeError):
        return default


@contextmanager
def _writer_slot():
    import _bootstrap
    root = Path(os.getenv('VALUATION_STATE_DIR') or Path(_bootstrap.DB_DIR) / 'provider_state')
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'valuation-writer.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class ValuationSynchronizer:
    def __init__(self, store):
        self.store = store

    def sync(self, day, symbols, *, bulk_only=False):
        """Historical bootstrap uses only bulk APIs, never N days * N stocks.

        Nightly and 20:05 runs each get <=480s; premarket gets <=120s. Paid
        providers also have their existing shared quota/cooldown guards. Atomic
        composite checkpoints retain partial rows and immutable replay evidence.
        """
        wanted = sorted(set(str(s) for s in symbols))
        started = time.monotonic()
        evening = datetime.now(TZ).hour >= 15
        seconds = _setting('VALUATION_EVENING_BUDGET_SECONDS' if evening else
                           'VALUATION_PREMARKET_BUDGET_SECONDS', 480 if evening else 120,
                           600 if evening else 180)
        deadline = started + seconds
        attempts = []
        with _writer_slot() as acquired:
            rows = self.store.load_valuation_evidence(day)
            if not acquired:
                return self._summary(rows, wanted, [{'provider': 'collector', 'status': 'busy'}])
            changed = False

            def merge(frame, provider):
                nonlocal rows, changed
                if frame is None or frame.empty:
                    return 0
                quality = frame.attrs.get('provenance', {}).get('quality_status', 'ok')
                if quality != 'ok':
                    return 0
                before = rows
                rows = merge_snapshot(rows, frame, day=day, provider=provider)
                changed = changed or rows != before
                return len(rows) - len(before)

            def checkpoint():
                nonlocal changed
                if not changed or not rows:
                    return
                records = [{**r, 'valuation_contract': 'composite-v1',
                            'quality_status': 'ok' if complete(r) else 'partial'} for r in rows]
                frame = pd.DataFrame(records)
                frame.attrs['provenance'] = {
                    'provider': 'valuation_composite', 'quality_status': 'ok',
                    'origin': 'same_day_field_merge', 'schema_version': 'valuation-composite-v1',
                }
                self.store.upsert_valuations(frame, as_of=day)
                changed = False

            def missing(fields):
                current = {r['symbol']: r for r in rows}
                return [s for s in wanted if any(number(current.get(s, {}).get(f)) in (None, 0)
                                                for f in fields)]

            # Stable, whole-market dated APIs first; one source failing never
            # prevents the others from being tried. No raw provider errors logged.
            for provider in ('zzshare', 'tushare'):
                if provider != 'zzshare' and not missing(('pe_ttm', 'pb', 'market_cap')):
                    break
                if time.monotonic() >= deadline:
                    break
                try:
                    frame = sources.historical(provider, day)
                    merge(frame, provider)
                    status = 'empty_or_unavailable'
                    if frame is not None and not frame.empty:
                        quality = frame.attrs.get('provenance', {}).get('quality_status', 'ok')
                        dated = frame.get('provider_effective_as_of', frame.get('trade_date', pd.Series(dtype=str)))
                        status = ('rejected_quality' if quality != 'ok' else
                                  'ok' if dated.map(iso_day).eq(day).any() else 'rejected_date')
                    attempts.append({'provider': provider, 'status': status,
                                     'received': len(frame) if frame is not None else 0})
                except Exception as exc:
                    attempts.append({'provider': provider, 'status': 'failed',
                                     'error_type': type(exc).__name__})
                checkpoint()

            # Cheap batch close snapshots before the serial historical catch-up.
            # Priority is resolved PER FIELD, independently of arrival order.
            if not bulk_only and datetime.now(TZ).date().isoformat() == day and evening:
                live_deadline = min(deadline, started + seconds * .35)
                for provider in ('tencent', 'mairui', 'moma', 'eastmoney'):
                    fields = ('pb',) if provider in ('mairui', 'moma') else ('pb', 'market_cap')
                    codes = missing(fields)
                    if not codes:
                        continue
                    chunk = 20 if provider in ('mairui', 'moma') else 80
                    cap = _setting('VALUATION_PAID_BATCHES_PER_SOURCE', 30, 100) if chunk == 20 else 80
                    calls, received, empty = 0, 0, 0
                    # Each fallback gets a slice; a slow first source must not
                    # consume every later provider's opportunity to fill holes.
                    per_source_end = min(live_deadline, time.monotonic() + seconds * .12)
                    status = 'budget_limited'
                    for offset in range(0, min(len(codes), cap * chunk), chunk):
                        if time.monotonic() >= per_source_end:
                            break
                        calls += 1
                        try:
                            frame = sources.live(provider, codes[offset:offset + chunk], day)
                            merge(frame, provider)
                            n = len(frame)
                            received += n
                            empty = empty + 1 if n == 0 else 0
                            status = 'ok' if n else 'empty_or_unavailable'
                        except Exception as exc:
                            status = 'failed:' + type(exc).__name__
                            break
                        if empty >= 2:
                            break
                    attempts.append({'provider': provider, 'status': status, 'calls': calls,
                                     'received': received})
                    checkpoint()

            if not bulk_only:
                codes = [s for s in missing(('pe_ttm', 'pb')) if s.startswith(('00', '30', '60', '68'))]
                cap = _setting('VALUATION_BAOSTOCK_SYMBOLS_PER_RUN', 4500, 5000)
                pending, counts, failures = [], Counter(), 0
                for symbol in codes[:cap]:
                    if time.monotonic() >= deadline - 17:
                        counts['time_budget'] += 1
                        break
                    frame = baostock.valuation_day(symbol, day)
                    status = frame.attrs.get('status', 'ok' if not frame.empty else 'empty')
                    counts[status] += 1
                    if status in {'budget_exhausted', 'cooldown', 'busy'}:
                        break
                    failures = failures + 1 if status in {'empty', 'failed'} else 0
                    if not frame.empty:
                        pending.append(frame)
                    # Composite observations are immutable: publishing all 5000
                    # rows every 100 responses amplifies storage dramatically.
                    # At most five in-loop snapshots per 5000-symbol run.
                    if len(pending) >= 1000:
                        merge(pd.concat(pending, ignore_index=True), 'baostock')
                        checkpoint()
                        pending = []
                    if failures >= 3:
                        break
                if pending:
                    merge(pd.concat(pending, ignore_index=True), 'baostock')
                attempts.append({'provider': 'baostock', 'counts': dict(counts),
                                 'requested': sum(v for k, v in counts.items() if k != 'time_budget')})
                checkpoint()
        result = self._summary(rows, wanted, attempts)
        from data.reliability_store import ReliabilityStore
        evidence = ReliabilityStore(self.store)
        current = {r['symbol']: r for r in rows}
        for field in ('pe_ttm', 'pb', 'market_cap'):
            gaps = [s for s in wanted if number(current.get(s, {}).get(field)) in (None, 0)]
            identity = f'{day}:{field}'
            old = evidence.get('valuation_gap', identity)
            value = {'day': day, 'field': field, 'missing_symbols': gaps, 'priority': 0,
                     'consumer': 'formal_selection', 'status': 'partial' if gaps else 'complete',
                     'retry_owner': 'existing_valuation_sync', 'additional_requests': 0}
            evidence.put('valuation_gap', identity, value, expected_revision=(old or {}).get('revision', 0))
        result['gap_tracking'] = 'persisted; existing bounded sync owns retries'
        result['valuation_elapsed_seconds'] = round(time.monotonic() - started, 2)
        log.info('valuation snapshot %s: usable=%s coverage=%.1f%% fields=%s attempts=%s',
                 day, result['valuation_rows'], result['valuation_coverage'] * 100,
                 result['valuation_field_coverage'], attempts)
        return result

    @staticmethod
    def _summary(rows, wanted, attempts):
        wanted = set(wanted)
        matching = [r for r in rows if r['symbol'] in wanted]
        usable = sum(complete(r) for r in matching)
        coverage = usable / len(wanted) if wanted else 0
        return {
            'valuation_rows': usable, 'valuation_stored_rows': len(rows),
            'valuation_coverage': round(coverage, 6),
            'valuation_quality_status': 'ok' if usable else 'unavailable',
            'valuation_field_coverage': {
                f: round(sum(number(r.get(f)) not in (None, 0) for r in matching) / len(wanted), 6)
                if wanted else 0 for f in ('pe_ttm', 'pb', 'market_cap')},
            'valuation_attempts': attempts,
        }
