"""Fund v2 acceptance tests: independent cash arithmetic and transactional replays."""

import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import uuid
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
import _bootstrap
import fund_db
import fund_data
import fund_dca
import fund_analysis
import fund_metrics
from fund import (
    fund_contracts as contracts,
    fund_ledger as ledger,
    fund_service as service,
)
from fund.fund_calendar import executions
from fund.fund_returns import return_frame


@pytest.fixture(params=["sqlite", "postgres"])
def db(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        path = tmp_path / "fund.db"
        monkeypatch.setattr(fund_db, "is_postgres", lambda: False)
        monkeypatch.setattr(fund_db, "_conn", lambda: sqlite3.connect(path, timeout=20))
        import core.db_compat as compat

        original = compat.upsert_sql
        monkeypatch.setattr(
            compat,
            "upsert_sql",
            lambda *a: (original(*a)[0].replace("%s", "?"), original(*a)[1]),
        )
    else:
        dsn = os.environ.get("FUND_TEST_PG_DSN")
        if not dsn:
            pytest.skip("Set FUND_TEST_PG_DSN to an isolated test database")
        import psycopg2
        from core.db_compat import _PGConnection

        schema = "test_fund_" + uuid.uuid4().hex
        admin = psycopg2.connect(dsn)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("CREATE SCHEMA " + schema)

        def connect():
            wrapped = _PGConnection.__new__(_PGConnection)
            wrapped._conn = psycopg2.connect(dsn)
            with wrapped._conn.cursor() as cur:
                cur.execute("SET search_path TO " + schema)
            return wrapped

        monkeypatch.setattr(fund_db, "is_postgres", lambda: True)
        monkeypatch.setattr(fund_db, "_conn", connect)
    fund_db.init_db()
    monkeypatch.setattr(fund_data, "fund_name", lambda c: "测试混合")
    monkeypatch.setattr(fund_data, "fund_type", lambda c: "混合型")
    yield fund_db
    if request.param == "postgres":
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA " + schema + " CASCADE")
        admin.close()


def row(code="000001", **values):
    return {
        "code": code,
        "txn_type": "申购",
        "nav": 1.0,
        "amount": 100.0,
        "trade_date": "2026-01-05",
        **values,
    }


def nav(values, dates=None, **columns):
    f = pd.DataFrame(
        {
            "date": pd.to_datetime(
                dates or pd.date_range("2026-01-01", periods=len(values), freq="MS")
            ),
            "unit_nav": values,
            **columns,
        }
    )
    return f


def test_dividend_split_returns_and_contribution_drawdown():
    f = nav([1.0, 0.9], cash_dividend=[0.0, 0.1])
    assert fund_metrics.evaluate(f)["total_return"] == pytest.approx(0.0)
    assert fund_metrics.evaluate(nav([1.0, 0.5], split_ratio=[1, 2]))[
        "total_return"
    ] == pytest.approx(0.0)
    r = fund_dca.dca_backtest(nav([1.0, 0.5]), 100, fee_rate=0)
    assert r["final_value"] == 150
    assert r["max_drawdown"] == pytest.approx(0.5)
    assert r["total_invested"] == 200
    assert r["annualized_irr"] < 0


def test_plan_calendar_holidays_cross_year_and_month_end():
    rows = executions(
        "2026-01-01",
        "2026-02-20",
        ["2026-01-05", "2026-01-28", "2026-02-02", "2026-02-20"],
        "monthly",
        28,
    )
    assert rows == [{"scheduled_date": "2026-01-28", "execution_date": "2026-01-28"}]
    rows = executions(
        "2026-10-05",
        "2026-10-19",
        ["2026-10-09", "2026-10-12", "2026-10-19"],
        "biweekly",
        1,
        "2026-10-05",
    )
    assert [r["execution_date"] for r in rows] == ["2026-10-09", "2026-10-19"]
    assert not fund_dca.is_due(
        {"period": "biweekly", "day_of": 1}, pd.Timestamp("2026-10-12"), []
    )
    with pytest.raises(ValueError):
        executions("2026-01-01", "2026-01-31", [], "biweekly", 1)


def test_signal_budget_and_frozen_hash():
    f = nav([1, 0.5, 0.75, 1.1])
    r = fund_dca.dca_backtest(
        f,
        100,
        fee_rate=0,
        strategy="price_position",
        total_budget=250,
        max_period_amount=120,
    )
    assert r["total_invested"] == 250
    assert r["highest_investment"] <= 120
    assert all(x["cash"] >= -0.01 for x in r["equity_curve"])
    assert (
        r["experiment_id"]
        == fund_dca.dca_backtest(
            f,
            100,
            fee_rate=0,
            strategy="price_position",
            total_budget=250,
            max_period_amount=120,
        )["experiment_id"]
    )
    assert (
        fund_dca.dca_backtest(f, 100, strategy="valuation")["status"] == "unsupported"
    )
    estimates = pd.DataFrame(
        {"known_at": ["2026-01-01T16:00:00+08:00"], "percentile": [10]}
    )
    assert (
        fund_dca.dca_backtest(f, 100, strategy="valuation", valuation_series=estimates)[
            "status"
        ]
        == "partial"
    )
    f.attrs["quality"] = {"status": "partial", "reasons": ["pagination_no_progress"]}
    assert fund_dca.dca_backtest(f, 100)["status"] == "partial"


def test_old_fees_validation_fifo_and_redemption(db):
    result = ledger.commit([row(amount=101.0, fee=1.0)])
    assert result["positions"][0]["shares"] == 100
    assert result["positions"][0]["cost"] == 101
    assert ledger.commit([row(amount=101.0, fee=1.0)])["skipped"] == 1
    result = ledger.commit([row(nav=2.0, amount=100.0, trade_date="2026-02-01")])
    assert result["positions"][0]["shares"] == 150
    result = ledger.commit(
        [
            row(
                txn_type="赎回",
                nav=2,
                amount=None,
                shares=110.0,
                fee=1.0,
                trade_date="2026-03-01",
            )
        ]
    )
    assert result["positions"][0]["shares"] == 40
    assert (
        result["positions"][0]["cost"] == 80
    )  # first 100 and then 10 shares removed FIFO
    with pytest.raises(ValueError):
        ledger.commit(
            [row(txn_type="赎回", shares=41, amount=None, trade_date="2026-04-01")]
        )
    with pytest.raises(ValueError):
        ledger.commit([row(txn_type="任意类型")])
    assert ledger.read()["positions"][0]["shares"] == 40


def test_accounts_channels_conflicts_concurrent_idempotency(db):
    rows = [
        row(account="A", channel="x", external_id="t1"),
        row(account="A", channel="y", external_id="t1"),
        row(account="B", channel="x", external_id="t1"),
    ]
    ledger.commit(rows)
    assert (
        len(ledger.read()["positions"]) == 4
    )  # zero default opening plus three accounts
    with pytest.raises(ValueError):
        ledger.commit([dict(rows[0], amount=101)])
    item = row(code="000002", external_id="concurrent")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ledger.commit([item]), range(4)))
    assert sum(r["imported"] for r in results) == 1
    assert fund_db.get_holdings()[-1]["shares"] == 100


def test_atomic_batch_preview_conflict_and_backfill(db):
    preview = ledger.preview([row()])
    ledger.commit([row(code="000002")])
    assert (
        ledger.commit([row()], preview["version"])["imported"] == 1
    )  # unrelated code does not invalidate preview
    preview = ledger.preview([row(nav=2, amount=100, trade_date="2026-03-01")])
    ledger.commit([row(nav=1.5, trade_date="2026-02-01")])
    with pytest.raises(ValueError):
        ledger.commit(
            [row(nav=2, amount=100, trade_date="2026-03-01")], preview["version"]
        )
    before = ledger.read()
    with pytest.raises(ValueError):
        ledger.commit(
            [row(code="000003"), row(txn_type="赎回", shares=999, amount=None)]
        )
    assert ledger.read() == before
    ledger.commit([row(nav=0.5, trade_date="2026-01-01")])
    assert ledger.read()["positions"][0]["shares"] == pytest.approx(366.6667)


def test_reversal_replay_preserves_original_and_catches_negative_history(db):
    item = row(external_id="buy")
    ledger.commit([item])
    original = ledger.normalize(item)["event_id"]
    ledger.commit(
        [
            row(
                txn_type="冲正",
                reverses=original,
                external_id="reverse",
                trade_date="2026-01-06",
            )
        ]
    )
    assert ledger.read()["positions"][0]["shares"] == 0
    with ledger.transaction() as cur:
        cur.execute("SELECT COUNT(*) FROM fund_ledger_events")
        assert cur.fetchone()[0] == 2
    with pytest.raises(ValueError):
        ledger.commit([row(txn_type="冲正", reverses=original, external_id="reverse2")])


def test_migration_and_legacy_boundary(db):
    fund_db.upsert_holding("000001", shares=100, cost_nav=1)
    with pytest.raises(ValueError):
        ledger.commit([row()])
    opening = [{"code": "000001", "shares": 100, "cost_nav": 1, "as_of": "2026-01-01"}]
    p = ledger.migration_preview(opening)
    ledger.migrate(opening, p["version"])
    ledger.commit([row()])
    assert fund_db.get_holdings()[0]["shares"] == 200


def test_distribution_split_reinvestment_and_fee_cash(db):
    ledger.commit([row()])
    ledger.commit([row(txn_type="现金分红", nav=0, amount=10, trade_date="2026-01-06")])
    ledger.commit([row(txn_type="拆分", ratio=2, nav=0, trade_date="2026-01-07")])
    assert ledger.read()["positions"][0]["shares"] == 200
    assert ledger.read()["positions"][0]["cost"] == 100
    ledger.commit(
        [row(txn_type="红利再投", shares=10, nav=1, amount=10, trade_date="2026-01-08")]
    )
    assert ledger.read()["positions"][0]["shares"] == 210


def test_due_is_pending_and_repeat_has_no_new_holdings(db, monkeypatch):
    pid = fund_db.add_plan(
        "000001",
        100,
        period="biweekly",
        day_of=1,
        anchor_date="2026-10-05",
        open_dates=["2026-10-05", "2026-10-12", "2026-10-19"],
        auto_record=True,
    )
    first = service.process_due_plans(date_from("2026-10-12"))
    assert len(first) == 1 and first[0]["status"] == "pending"
    assert service.process_due_plans(date_from("2026-10-12")) == []
    assert fund_db.get_holdings() == []
    key = first[0]["occurrence_id"]
    ledger.commit(
        [row(trade_date="2026-10-12", occurrence_id=key, external_id="receipt")]
    )
    assert service.pending_plans() == []
    assert fund_db.get_holdings()[0]["shares"] == 100


def date_from(value):
    from datetime import date

    return date.fromisoformat(value)


def test_daily_pnl_same_across_web_mcp_and_job(db, monkeypatch):
    from portfolio.daily_pnl import calculate_fund_pnl

    # The web/MCP entry points use the current day. Keep their clock on the
    # fixture's NAV date instead of expiring this assertion after that date.
    monkeypatch.setattr(service, "now", lambda: "2026-10-15T18:00:00+08:00")
    ledger.commit([row()])
    f = nav([1, 1.1], dates=["2026-10-14", "2026-10-15"], cash_dividend=[0, 0])
    f.attrs.update(actions_status="ready", source="fixture")
    fund_db.save_nav("000001", f)
    snap = service.holdings_snapshot("2026-10-15")
    assert snap["confirmed_daily_pnl"] == 10
    daily = calculate_fund_pnl(
        fund_db.get_holdings(), fund_db.get_nav_pairs(), "2026-10-15"
    )
    assert daily["fund_daily_pnl"] == snap["confirmed_daily_pnl"]
    import webui.api_server as web
    import mcp_server

    assert web.fund_holdings()["data"] == mcp_server.fund_holdings()["holdings"]
    assert web.fund_holdings()["data"][0]["today_pnl"] == 10
    with pytest.raises(ValueError):
        fund_db.upsert_nav("000001", None, 1.0)


def test_daily_transactions_and_late_dates():
    h = {"shares": 150}
    info = {"unit_nav": 1.1, "prev_nav": 1.0, "nav_date": "2026-10-15"}
    result = contracts.daily_result(
        h, info, "2026-10-15", [{"delta_shares": 50, "cash_paid": 55}]
    )
    assert result["today_pnl"] == 10
    assert contracts.daily_result(h, info, "2026-10-16")["today_pnl"] is None


def test_metrics_maturity_and_required_screen_fields(db, monkeypatch):
    import fund_screener

    f = nav([1, 1.1], daily_return=[0, 10])
    score = fund_analysis.score_fund("000001", nav_df=f)
    assert score["grade"] is None and score["metrics"]["annualized_return"] is None
    monkeypatch.setattr(
        fund_data,
        "get_rank",
        lambda _: pd.DataFrame(
            {"基金代码": ["000001"], "基金简称": ["示例"], "近1年": [5.0]}
        ),
    )
    with pytest.raises(ValueError):
        fund_screener.screen_funds(max_fee=1.0)
    monkeypatch.setattr(
        fund_data,
        "get_rank",
        lambda _: pd.DataFrame(
            {
                "基金代码": ["000001", "000002"],
                "近1年": [5.0, 6.0],
                "手续费": [None, 1.0],
            }
        ),
    )
    assert [r["code"] for r in fund_screener.screen_funds(max_fee=1.0)] == ["000002"]


def test_experiment_frozen_replay_and_api_preview(db, monkeypatch):
    f = nav([1, 0.5, 0.75], cash_dividend=[0, 0, 0])
    f.attrs["actions_status"] = "ready"
    monkeypatch.setattr(fund_data, "get_nav_history", lambda *a, **k: f)
    original = service.run_experiment("000001", amount=100, fee_rate=0)
    monkeypatch.setattr(
        fund_data,
        "get_nav_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    repeated = service.replay_experiment(original["experiment_id"])
    assert repeated["experiment_id"] == original["experiment_id"]
    assert repeated["final_value"] == original["final_value"]
    from webui.fund_routes import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/fund/ledger/preview", json={"rows": [row()]}
        ).json()
        assert response["ok"]
        response = client.post(
            "/api/fund/ledger/commit",
            json={"rows": [row()], "expected_version": response["data"]["version"]},
        ).json()
        assert response["ok"] and response["data"]["imported"] == 1
        assert (
            client.post("/api/fund/ledger/commit", json={"rows": [row()]}).json()["ok"]
            is False
        )


def test_portfolio_unknown_exposure_and_budgeted_preview(db, monkeypatch):
    import fund_portfolio

    monkeypatch.setattr(fund_data, "get_stock_holdings", lambda *a, **k: None)
    holdings = [
        {
            "code": "000001",
            "shares": 100,
            "mv": 100,
            "profile": {
                "asset": "权益",
                "top_holdings": {
                    "quarter": "2026Q2",
                    "holdings": [{"code": "600001", "name": "测试", "pct": 10}],
                },
            },
            "nav_date": "2026-10-15",
        }
    ]
    diagnosis = fund_portfolio.diagnose(holdings, with_overlap=True)
    assert diagnosis["disclosure"]["unknown_weight"] == pytest.approx(0.9)
    result = fund_portfolio.rebalance_preview(
        {"holdings": holdings, "as_of": "2026-10-15"},
        {"000002": 1},
        50,
        fee_rules={"000002": {"purchase_rate": 0.0015}},
    )
    assert result["estimated_cash_used"] <= 50
    assert any(
        "持有期" in str(r["blockers"])
        for r in result["orders"]
        if r["action"] == "redeem"
    )


def test_actual_due_renderer_long_report_delivers_once(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from application.message_archive import MessageArchiveService
    from data.research_store import ResearchStore
    from notify import notification_router as router

    rows = [
        {
            "code": f"{i:06}",
            "name": "测试基金" * 12,
            "amount": 100,
            "scheduled_date": "2026-10-15",
        }
        for i in range(120)
    ]
    payload = service.render_due(rows)
    assert len(payload) > 8000 and "000119" in payload
    store = ResearchStore(str(tmp_path / "archive.db"), connect_fn=sqlite3.connect,
                          is_postgres=False, ensure_schema=True)
    archive = MessageArchiveService(store=store, key=Fernet.generate_key())
    monkeypatch.setattr(router, "archive_action", lambda action, body: (
        archive.prepare(**body) if action == "prepare" else
        {"started": archive.start(body["delivery_id"])} if action == "start" else
        {"recorded": archive.finish(**body)}))
    sent = []
    monkeypatch.setitem(router.CHANNELS, "qq", lambda title, body: (
        sent.append(body) or (True, "HTTP 200")))
    first = router.send(
        "report",
        "基金定投待确认",
        payload,
        source="fund.plan_due",
        idempotency_key="fund-due:fixture",
        compact=False,
        only_channels=["qq"],
    )
    second = router.send(
        "report",
        "基金定投待确认",
        payload,
        source="fund.plan_due",
        idempotency_key="fund-due:fixture",
        compact=False,
        only_channels=["qq"],
    )
    assert first["qq"] == (True, "HTTP 200") and first.archive_status == "recorded"
    assert second["qq"] == (False, "archive_suppressed")
    assert sent == [payload]
    assert archive.detail(first.message_id)["original_body"] == payload


def test_partial_confirmation_reversal_and_projection_migration(db):
    fund_db.add_plan(
        "000001",
        100,
        period="monthly",
        day_of=1,
        anchor_date="2026-01-01",
        open_dates=["2026-01-05"],
    )
    item = service.process_due_plans(date_from("2026-01-05"))[0]
    a = row(amount=40, occurrence_id=item["occurrence_id"], external_id="partial-1")
    b = row(amount=60, occurrence_id=item["occurrence_id"], external_id="partial-2")
    ledger.commit([a])
    assert service.pending_plans()[0]["status"] == "partial"
    ledger.commit([b])
    assert service.pending_plans() == []
    ledger.commit(
        [
            row(
                txn_type="冲正",
                trade_date="2026-01-06",
                reverses=ledger.normalize(a)["event_id"],
                external_id="reverse-partial",
            )
        ]
    )
    assert service.pending_plans()[0]["confirmed_amount"] == "60.0"
    assert fund_db.get_holdings()[0]["shares"] == 60
    opening = [{"code": "000001", "shares": 10, "cost_nav": 1, "as_of": "2026-01-01"}]
    preview = ledger.migration_preview(opening)
    ledger.migrate(opening, preview["version"])
    assert fund_db.get_holdings()[0]["shares"] == 70


def test_fee_schedule_and_share_class_scenario():
    from fund.fund_fees import redemption_charge, share_class_comparison

    schedule = [
        {"min_days": 0, "rate": 0.015},
        {"min_days": 7, "rate": 0.005},
        {"min_days": 30, "rate": 0},
    ]
    result = redemption_charge(
        [{"date": "2026-01-01", "shares": 100}, {"date": "2026-01-28", "shares": 100}],
        1,
        "2026-02-01",
        schedule,
    )
    assert result["fee"] == 1.5
    with pytest.raises(ValueError):
        redemption_charge([{"date": None, "shares": 100}], 1, "2026-02-01", schedule)
    classes = [
        {
            "code": "000001",
            "family_id": "family",
            "purchase_rate": 0.0015,
            "annual_service_rate": 0,
            "redemption_schedule": schedule,
        },
        {
            "code": "000002",
            "family_id": "family",
            "purchase_rate": 0,
            "annual_service_rate": 0.004,
            "redemption_schedule": schedule,
        },
    ]
    result = share_class_comparison(classes, 10000, 365)
    assert (
        result["classes"][0]["estimated_cost"] < result["classes"][1]["estimated_cost"]
    )
    r = fund_dca.dca_backtest(
        nav([1.0, 1.0]), 100, fee_rate=0, redemption_fee_schedule=schedule
    )
    assert r["total_fee"] == 1.5


def test_cash_dividend_receivable_and_money_income():
    f = nav(
        [1, 0.9, 0.9],
        dates=["2026-01-01", "2026-01-02", "2026-02-01"],
        cash_dividend=[0, 0.1, 0],
    )
    f.attrs.update(
        actions_status="ready",
        actions={
            "events": [
                {
                    "date": "2026-01-02",
                    "kind": "dividend",
                    "value": 0.1,
                    "payment_date": "2026-01-05",
                }
            ]
        },
    )
    result = fund_dca.dca_backtest(f, 100, fee_rate=0)
    assert result["equity_curve"][1]["receivable"] == 10
    assert result["equity_curve"][-1]["receivable"] == 0
    assert result["profit"] == 0
    money = nav([1.0, 1.0], income_per_10000=[0.5, 0.6])
    assert return_frame(money).attrs["return_basis"] == "money_income"
    h = {"shares": 10000}
    info = {
        "unit_nav": 1,
        "prev_nav": 1,
        "income_per_10000": 0.6,
        "unit_nav_kind": "accounting_reference",
        "nav_date": "2026-10-15",
    }
    assert contracts.daily_result(h, info, "2026-10-15")["today_pnl"] == 0.6


def test_missing_actions_remain_partial_and_short_irr_unknown():
    f = nav([1.0, 1.1], daily_return=[0, 10])
    f.attrs["actions_status"] = "partial"
    assert fund_dca.dca_backtest(f, 100)["status"] == "partial"
    assert (
        fund_dca.xirr(
            [(pd.Timestamp("2026-01-01"), -100), (pd.Timestamp("2026-01-02"), 101)]
        )
        is None
    )
    with pytest.raises(ValueError):
        contracts.code6("")


def test_class_specific_scores_and_research_changes(db, monkeypatch):
    dates = pd.bdate_range("2024-01-01", periods=400)
    values = (1 + np.random.default_rng(8).normal(0.0003, 0.004, 400)).cumprod()
    frame = pd.DataFrame(
        {"date": dates, "unit_nav": values, "cash_dividend": 0.0, "split_ratio": 1.0}
    )
    frame.attrs["actions_status"] = "ready"
    equity = fund_analysis.score_fund(
        "000001", frame, profile=contracts.product("000001", "主动混合", "混合型")
    )
    bond = fund_analysis.score_fund(
        "000002", frame, profile=contracts.product("000002", "债券", "债券型")
    )
    assert (
        equity["weights"] != bond["weights"]
        and equity["status"] == bond["status"] == "ready"
    )
    monkeypatch.setattr(fund_data, "get_nav_history", lambda *a, **k: frame)
    state = {"manager": "经理甲"}
    monkeypatch.setattr(
        fund_data, "get_fund_basic", lambda c: {"基金经理": state["manager"]}
    )
    monkeypatch.setattr(
        fund_data, "top_holdings", lambda c: {"quarter": "2026Q2", "holdings": []}
    )
    first = service.research_context("000001")
    state["manager"] = "经理乙"
    second = service.research_context("000001", refresh=True)
    assert second["profile"]["changes"][0]["before"] == "经理甲"
    assert second["profile"]["changes"][0]["after"] == "经理乙"
    assert second["artifact"]["formal"] is False
    assert second["artifact"]["invalidation_conditions"]


def test_plan_dedup_does_not_merge_different_accounts_and_dates(db, monkeypatch):
    first = {
        "code": "000001",
        "amount": 100,
        "anchor_date": "2026-01-01",
        "account": "A",
    }
    second = {**first, "account": "B"}
    result = fund_db.import_plans([first, second])
    assert result["imported"] == 2
    assert fund_db.import_plans([first, second])["skipped"] == 2
    assert len(fund_db.get_plans()) == 2


def test_no_cost_price_replacement_and_no_destructive_ledger_shortcut(db):
    ledger.commit([row()])
    snapshot = service.holdings_snapshot("2026-10-15")
    assert snapshot["holdings"][0]["mv"] is None
    assert snapshot["confirmed_daily_pnl"] is None
    with pytest.raises(ValueError):
        fund_db.delete_holding("000001")
    with pytest.raises(ValueError):
        fund_db.upsert_holding("000001", shares=500)
    f = nav([1.0, -2.0], dates=["2026-10-14", "2026-10-15"])
    fund_db.save_nav("000001", f)
    assert fund_db.get_latest_navs()["000001"]["nav_date"] == "2026-10-14"


def test_account_market_valuation_dedup_and_cost_fallback_exclusion():
    from fund.fund_portfolio import merge_market_values

    funds = [
        {
            "code": "510300",
            "mv": 100,
            "nav_date": "2026-10-15",
            "profile": {"asset": "权益"},
        }
    ]
    stocks = [
        {"code": "510300", "mv": 100, "price_source": "quote"},
        {"code": "600001", "mv": 50, "price_source": "cost_missing"},
        {"code": "600002", "mv": 30, "price_source": "quote"},
    ]
    result = merge_market_values(funds, stocks, "2026-10-15", "2026-10-15")
    assert result["total"] == 130
    assert result["excluded_duplicates"][0]["code"] == "510300"
    assert result["unvalued"] == ["600001"]


def test_weekly_review_retries_partial_and_deduplicates_completed(db, monkeypatch):
    ledger.commit([row()])
    monkeypatch.setattr(fund_data, "get_fund_basic", lambda c: None)
    monkeypatch.setattr(fund_data, "top_holdings", lambda c: {"holdings": []})
    first = service.review_week("2026-10-15")
    assert first[0]["status"] == "partial"
    assert not ledger.list_saved("weekly_review")
    monkeypatch.setattr(fund_data, "get_fund_basic", lambda c: {"基金经理": "匿名经理"})
    assert service.review_week("2026-10-15")[0]["status"] == "ready"
    assert service.review_week("2026-10-15") == []


def test_new_plan_settings_are_atomic_and_frozen_after_occurrence(db):
    pid = fund_db.add_plan(
        "000001",
        100,
        period="monthly",
        day_of=1,
        anchor_date="2026-01-01",
        open_dates=["2026-01-05"],
    )
    assert (
        service.configure_plan(pid, {"anchor_date": "2026-01-01"})["anchor_date"]
        == "2026-01-01"
    )
    service.process_due_plans(date_from("2026-01-05"))
    with pytest.raises(ValueError):
        service.configure_plan(pid, {"anchor_date": "2026-02-01"})


def test_content_fingerprint_ignores_source_notes_and_dates_use_shanghai(db):
    first = row(name="匿名", note="首次录入", source="manual")
    ledger.commit([first])
    assert (
        ledger.commit([dict(first, name="更新名称", note="账单导入", source="import")])[
            "skipped"
        ]
        == 1
    )
    dated = ledger.normalize(row(trade_date="2026-01-01T18:00:00Z"))
    assert dated["confirmation_date"] == "2026-01-02"


def test_explicit_execution_calendar_requires_matching_nav():
    f = nav([1.0, 1.1], dates=["2026-01-01", "2026-02-01"])
    result = fund_dca.dca_backtest(f, 100, day=5, open_dates=["2026-01-05"])
    assert result["status"] == "partial" and "对应净值" in result["error"]


def test_standard_postgres_migration_records_version_idempotently(db):
    if not fund_db.is_postgres():
        return  # Production migrations target PostgreSQL; SQLite is only an injected calculator test.
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts/migrations/22-fund-research-upgrade.sql"
    ).read_text()
    with ledger.transaction(True) as cur:
        cur.execute(
            "CREATE TABLE research_schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT)"
        )
        cur.execute(script)
        cur.execute(script)
        cur.execute(
            "SELECT COUNT(*) FROM research_schema_migrations WHERE version='22-fund-research-upgrade'"
        )
        assert cur.fetchone()[0] == 1


def test_replay_does_not_silently_apply_new_calculation_rules(db, monkeypatch):
    frame = nav([1.0, 1.1], cash_dividend=[0.0, 0.0])
    frame.attrs["actions_status"] = "ready"
    monkeypatch.setattr(fund_data, "get_nav_history", lambda *a, **k: frame)
    original = service.run_experiment("000001", amount=100)
    monkeypatch.setattr(contracts, "engine_hash", lambda: "different-rules")
    replayed = service.replay_experiment(original["experiment_id"])
    assert replayed["status"] == "unsupported"
    assert (
        ledger.list_saved("experiment")[0]["result"]["engine_hash"]
        == original["engine_hash"]
    )


def test_initial_capital_comparison_uses_same_redemption_fee_schedule():
    frame = nav([1.0, 1.0], dates=["2026-01-01", "2026-01-03"])
    result = fund_dca.dca_backtest(
        frame,
        100,
        fee_rate=0,
        redemption_fee_schedule=[
            {"min_days": 0, "rate": 0.015},
            {"min_days": 7, "rate": 0},
        ],
    )
    assert result["final_value"] == 98.5
    assert result["lump_sum"]["final_value"] == 98.5


def test_unsettled_subscription_is_a_liability_not_free_profit():
    snapshot = {
        "as_of": "2026-10-15",
        "valuation_count": 1,
        "total_count": 1,
        "total_mv": 110,
    }
    books = {
        "history": [{"history_status": "complete_from_zero"}],
        "transactions": [
            {
                "confirmation_date": "2026-10-15",
                "settlement_date": "2026-10-16",
                "cash_paid": 100,
                "cash_received": 0,
            }
        ],
    }
    result = service.investor_performance(snapshot, books)
    assert result["net_profit"] == 10
    assert result["annualized_irr"] is None
