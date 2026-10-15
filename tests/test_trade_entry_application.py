from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from application.run_repository import RunRepository
from application.services import ApplicationError, PortfolioAccessService, TradeEntryService
from portfolio.portfolio_db_pg import PortfolioDBPG, _trade_time_for_pg


class FakePortfolioDB:
    def __init__(self):
        self.holdings = []
        self.trades = []
        self.import_calls = 0
        self.batches = {}

    def get_all_stocks(self):
        return self.holdings

    def get_trades(self, _code=None, _limit=10000):
        return self.trades

    def import_trades(self, rows, update_position=True):
        self.import_calls += 1
        self.trades.extend(dict(row) for row in rows)
        return {
            "imported": len(rows), "failed": 0,
            "positions_updated": len(rows) if update_position else 0,
            "errors": [],
        }

    def stage_trade_import_batch(self, *, batch_id, actor_id, preview_hash,
                                 position_watermark, update_position, rows):
        self.batches.setdefault(batch_id, {
            "batch_id": batch_id, "actor_id": actor_id, "status": "staged",
            "update_position": update_position, "preview_hash": preview_hash,
            "position_watermark": position_watermark, "row_count": len(rows),
            "created_at": "2026-09-01T12:00:00+08:00",
            "confirmed_at": None, "abandoned_at": None,
            "rows": [
                {"row_number": index, "normalized_payload": dict(row)}
                for index, row in enumerate(rows, 1)
            ],
        })

    def get_trade_import_batch(self, batch_id, actor_id=None):
        value = self.batches.get(batch_id)
        if value is None or (actor_id and value["actor_id"] != actor_id):
            return None
        return value

    def list_trade_import_batches(self, actor_id, status=None, limit=100):
        return [
            value for value in self.batches.values()
            if value["actor_id"] == actor_id and (status is None or value["status"] == status)
        ][:limit]

    def mark_trade_import_batch(self, batch_id, status):
        if batch_id in self.batches:
            self.batches[batch_id]["status"] = status

    def abandon_trade_import_batch(self, batch_id, actor_id):
        value = self.get_trade_import_batch(batch_id, actor_id)
        if value is None or value["status"] != "staged":
            return False
        value["status"] = "abandoned"
        return True


@pytest.fixture()
def service(tmp_path):
    database = tmp_path / "trade-actions.db"

    def connect(_name=""):
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    db = FakePortfolioDB()
    repository = RunRepository(connect_fn=connect, is_postgres=False)
    return TradeEntryService(repository, portfolio_db=db), db


def _row(price=1500):
    return {
        "code": "600519", "name": "贵州茅台", "trade_type": "买入",
        "quantity": 100, "price": price, "trade_time": "2026-08-21 10:00:00",
        "commission": 5, "tax": 2.5, "note": "example entry",
    }


def test_trade_entry_requires_matching_preview_and_is_idempotent(service) -> None:
    entry, db = service
    preview = entry.preview(rows=[_row()], update_position=True)
    assert preview["status"] == "ready"
    assert preview["rows"][0]["source"] == "web:trade-entry"
    assert preview["rows"][0]["commission"] == 5
    assert preview["rows"][0]["tax"] == 2.5
    assert preview["rows"][0]["note"] == "example entry"

    result = entry.confirm(
        actor_id="shadow-user-example",
        idempotency_key="trade-entry-example-key",
        preview_hash=preview["preview_hash"],
        rows=[_row()],
        update_position=True,
        confirmed=True,
    )
    repeated = entry.confirm(
        actor_id="shadow-user-example",
        idempotency_key="trade-entry-example-key",
        preview_hash=preview["preview_hash"],
        rows=[_row()],
        update_position=True,
        confirmed=True,
    )
    assert result == repeated
    assert result["imported"] == 1
    assert db.import_calls == 1


def test_utc_db_session_preview_confirm_keeps_shanghai_execution_time(tmp_path, monkeypatch) -> None:
    """Exercise the application boundary and PG INSERT binding without production data."""
    from portfolio import portfolio_db_pg as pg

    stored = []

    class Cursor:
        def execute(self, sql, params=None):
            if 'INSERT INTO trade_records' in sql:
                assert 'COALESCE(%s::timestamptz, NOW())' in sql
                bound = params[9]
                assert isinstance(bound, datetime) and bound.utcoffset() is not None
                stored.append({
                    'trade_time': bound.astimezone(timezone.utc),
                    'external_fingerprint': params[20],
                })

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(pg, 'get_conn', Connection)
    db = PortfolioDBPG.__new__(PortfolioDBPG)
    db.get_all_stocks = lambda: []
    db.get_trades = lambda *_args: []
    db.existing_trade_execution_keys = lambda keys, **_kwargs: [
        key for key in keys if key in {item['external_fingerprint'] for item in stored}
    ]
    db.stage_trade_import_batch = lambda **_kwargs: None
    db.mark_trade_import_batch = lambda *_args: None

    database = tmp_path / 'trade-time-actions.db'

    def connect(_name=''):
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    entry = TradeEntryService(RunRepository(connect_fn=connect, is_postgres=False),
                              portfolio_db=db)
    row = {**_row(), 'trade_time': '2026-09-18 13:26:46'}
    preview = entry.preview(rows=[row], update_position=False)
    assert preview['status'] == 'ready'
    first = entry.confirm(
        actor_id='user-1', idempotency_key='trade-time-naive-1',
        preview_hash=preview['preview_hash'], rows=[row],
        update_position=False, confirmed=True,
    )
    assert first['imported'] == 1
    assert stored[0]['trade_time'].isoformat() == '2026-09-18T05:26:46+00:00'
    assert stored[0]['trade_time'].astimezone(ZoneInfo('Asia/Shanghai')).strftime(
        '%Y-%m-%d %H:%M:%S') == '2026-09-18 13:26:46'

    repeated = entry.confirm(
        actor_id='user-1', idempotency_key='trade-time-naive-2',
        preview_hash=preview['preview_hash'], rows=[row],
        update_position=False, confirmed=True,
    )
    assert repeated['status'] == 'noop'
    assert repeated['skipped_existing'] == 1
    assert len(stored) == 1

    offset_row = {**row, 'trade_time': '2026-09-18 13:26:46+00:00'}
    offset_preview = entry.preview(rows=[offset_row], update_position=False)
    offset = entry.confirm(
        actor_id='user-1', idempotency_key='trade-time-offset-1',
        preview_hash=offset_preview['preview_hash'], rows=[offset_row],
        update_position=False, confirmed=True,
    )
    assert offset['imported'] == 1
    assert stored[1]['trade_time'].isoformat() == '2026-09-18T13:26:46+00:00'
    assert stored[0]['external_fingerprint'] != stored[1]['external_fingerprint']


def test_trade_timestamp_absent_keeps_now_fallback() -> None:
    assert _trade_time_for_pg(None) is None
    assert _trade_time_for_pg('') is None


def test_trade_preview_rejects_invalid_fees(service) -> None:
    entry, db = service
    row = _row()
    row["commission"] = -1
    preview = entry.preview(rows=[row])
    assert preview["status"] == "needs_input"
    assert preview["prepared"] == 0
    assert preview["errors"]
    assert db.import_calls == 0


def test_trade_entry_rejects_unconfirmed_mismatch_and_key_reuse(service) -> None:
    entry, db = service
    preview = entry.preview(rows=[_row()])
    with pytest.raises(ApplicationError) as unconfirmed:
        entry.confirm(
            actor_id="shadow-user-example", idempotency_key="trade-entry-example-key",
            preview_hash=preview["preview_hash"], rows=[_row()], confirmed=False,
        )
    assert unconfirmed.value.code == "confirmation_required"
    with pytest.raises(ApplicationError) as mismatch:
        entry.confirm(
            actor_id="shadow-user-example", idempotency_key="trade-entry-example-key",
            preview_hash="wrong-preview", rows=[_row()], confirmed=True,
        )
    assert mismatch.value.code == "preview_mismatch"

    entry.confirm(
        actor_id="shadow-user-example", idempotency_key="trade-entry-example-key",
        preview_hash=preview["preview_hash"], rows=[_row()], confirmed=True,
    )
    changed = entry.preview(rows=[_row(price=1600)])
    with pytest.raises(ApplicationError) as conflict:
        entry.confirm(
            actor_id="shadow-user-example", idempotency_key="trade-entry-example-key",
            preview_hash=changed["preview_hash"], rows=[_row(price=1600)], confirmed=True,
        )
    assert conflict.value.status_code == 409
    assert db.import_calls == 1


def test_trade_entry_preview_binds_position_watermark_and_effects(service) -> None:
    entry, db = service
    db.holdings = [{
        "code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1400,
    }]
    row = _row()
    row["trade_type"] = "卖出"
    preview = entry.preview(rows=[row], actor_id="shadow-user-example")
    assert preview["status"] == "ready"
    assert preview["batch_id"].startswith("tb_")
    assert len(preview["position_watermark"]) == 64
    assert preview["effects"][0]["position_before"] == 100
    assert preview["effects"][0]["position_after"] == 0


def test_nexus_trade_review_stages_commits_and_replays(service) -> None:
    entry, db = service
    fields = {"tradesJson": __import__("json").dumps([_row()]), "updatePosition": "true"}
    created = entry.create_review(
        actor_id="nexus-agent", fields=fields,
        idempotency_key="nexus-review-create-0001", trace_id="trace-1",
    )
    repeated_create = entry.create_review(
        actor_id="nexus-agent", fields=fields,
        idempotency_key="nexus-review-create-0001",
    )
    assert created["protocol"] == "shadow.review.v1"
    assert created["state"] == "pending"
    assert repeated_create["review_id"] == created["review_id"]
    assert repeated_create["replayed"] is True

    with pytest.raises(ApplicationError) as conflict:
        entry.create_review(
            actor_id="nexus-agent",
            fields={"trades": [_row(price=1600)]},
            idempotency_key="nexus-review-create-0001",
        )
    assert conflict.value.status_code == 409

    committed = entry.commit_review(
        created["review_id"], actor_id="nexus-agent",
        idempotency_key="nexus-trade-import-0001",
    )
    replayed_commit = entry.commit_review(
        created["review_id"], actor_id="nexus-agent",
        idempotency_key="nexus-trade-import-0001",
    )
    assert committed["state"] == "approved"
    assert committed["receipt"].startswith("shadow://foliant/trade-imports/")
    assert replayed_commit["replayed"] is True
    assert db.import_calls == 1


def test_portfolio_access_distinguishes_import_and_trade_dates() -> None:
    db = FakePortfolioDB()
    db.trades = [{
        **_row(), "created_at": "2026-09-01 09:48:14",
        "trade_time": datetime.fromisoformat("2026-08-28T10:38:28+08:00"),
    }]
    access = PortfolioAccessService(portfolio_db=db)
    imported = access.trades(import_date="2026-09-01")
    executed = access.trades(trade_date="2026-09-01")
    assert imported["data"]["count"] == 1
    assert executed["data"]["count"] == 0
