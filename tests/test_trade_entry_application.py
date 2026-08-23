from __future__ import annotations

import sqlite3

import pytest

from application.run_repository import RunRepository
from application.services import ApplicationError, TradeEntryService


class FakePortfolioDB:
    def __init__(self):
        self.holdings = []
        self.trades = []
        self.import_calls = 0

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
