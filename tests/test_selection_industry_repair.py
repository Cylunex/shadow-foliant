from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd
import pytest

from analysis.independent_selector import IndependentPolicy, _score, build
from analysis.local_stock_selector import SelectionPolicy
from analysis.local_fusion import FusionPolicy
from application.industry_classification import classify_symbols
from application.services import SelectionRunService
from application.scheduled_snapshot import ScheduledSnapshotService
from data.research_store import ResearchStore
from data.research_sync import ResearchSynchronizer
from data.security_master import prepare_master
from data.selection_quality import input_quality


def store_at(tmp_path):
    return ResearchStore(connect_fn=lambda _path: sqlite3.connect(tmp_path / "research.sqlite"),
                         is_postgres=False)


def master(day, symbols=("600001", "600002"), *, lifecycle=False):
    rows = [{"ts_code": symbol + ".SH", "name": "样本", "industry": "银行",
             "exchange": "SSE", "list_status": "L", "list_date": "2000-01-01"}
            for symbol in symbols]
    frame = pd.DataFrame(rows)
    frame.attrs = {
        "provenance": {"provider": "test", "as_of": day,
                       "retrieved_at": day + "T18:10:00+08:00"},
        "available_list_statuses": ["L"], "lifecycle_complete": lifecycle,
    }
    return frame


def test_current_publication_does_not_silently_enable_lifecycle_backfill(tmp_path):
    store = store_at(tmp_path)
    first = master("2026-09-18", lifecycle=True)
    first.loc[len(first)] = {"ts_code": "600003.SH", "name": "已退市", "exchange": "SSE",
                             "list_status": "D", "list_date": "2000-01-01", "industry": "电子"}
    first["delist_date"] = ["", "", "2025-01-01"]
    first.attrs["available_list_statuses"] = ["L", "D"]
    old = store.publish_security_master(first, minimum_rows=1)
    current = master("2026-09-21")
    rejected = store.publish_security_master(current, minimum_rows=1)
    assert not rejected["published"]
    fresh = store.publish_security_master(current, minimum_rows=1,
                                           universe_scope="current_listed")
    assert fresh["published"]
    live = store.load_universe("2026-09-22")
    assert live.attrs["snapshot_id"] == fresh["snapshot_id"]
    assert live.attrs["universe_scope"] == "current_listed"
    assert store.load_universe("2026-09-19").attrs["snapshot_id"] == old["snapshot_id"]
    history = store.load_lifecycle_universe("2024-12-31")
    assert history.attrs["snapshot_id"] == old["snapshot_id"]
    assert "600003" in set(history["symbol"])
    assert history.attrs["lifecycle_complete"]


def test_current_membership_requires_provider_listed_evidence(tmp_path):
    frame = master("2026-09-21")
    frame.attrs["available_list_statuses"] = []
    result = store_at(tmp_path).publish_security_master(frame, minimum_rows=1,
                                                       universe_scope="current_listed")
    assert not result["published"]
    assert "current_listed_membership_unverified" in result["reasons"]


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration is opt-in")
def test_current_and_lifecycle_scope_contract_on_native_postgres(tmp_path, monkeypatch):
    import sys
    import uuid
    from psycopg2 import sql
    from core.db_compat import _PGConnection
    schema = "test_master_scopes_" + uuid.uuid4().hex
    admin = _PGConnection()
    admin._conn.cursor().execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.commit()

    def connect(_path=None):
        conn = _PGConnection()
        conn._conn.cursor().execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        store = ResearchStore(connect_fn=connect, is_postgres=True)
        monkeypatch.setattr(sys.modules[__name__], "store_at", lambda _: store)
        test_current_publication_does_not_silently_enable_lifecycle_backfill(tmp_path)
    finally:
        admin._conn.cursor().execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()


def test_full_market_classification_is_not_truncated_and_obeys_observation():
    symbols = [f"{600000 + i}" for i in range(150)]
    history = tuple({"symbol": symbol, "industry_code": "480101",
                     "effective_at": "2020-01-01 00:00:00",
                     "updated_at": "2020-01-02 00:00:00"} for symbol in symbols)
    source = {"observed_at": "2026-09-20T10:00:00+08:00", "csv_sha256": "original"}
    with patch("application.industry_classification._source", return_value=(history, source)):
        result = classify_symbols(symbols, as_of="2026-09-21T10:00:00+08:00")
        assert len(result["rows"]) == 150
        assert result["coverage"] == 1
        assert classify_symbols(symbols, as_of="2026-09-19")["status"] == "missing"


def test_industry_evidence_is_frozen_in_master_not_reloaded_at_selection(tmp_path):
    day = datetime.now().date().isoformat()
    frame = master(day)
    rows = [{"symbol": symbol, "industry_l1_name": "电子", "industry_l1_code": "27",
             "effective_at": "2020-01-01 00:00:00"} for symbol in ("600001", "600002")]
    classification = {"status": "complete", "coverage": 1, "rows": rows,
                      "source": {"csv_sha256": "frozen-sha", "observed_at": "2020-01-01"}}
    with patch("application.industry_classification.classify_symbols", return_value=classification):
        prepared = prepare_master(frame)
    assert prepared.attrs["universe_scope"] == "current_listed"
    store = store_at(tmp_path)
    result = store.publish_security_master(prepared, minimum_rows=1, universe_scope="current_listed")
    assert result["published"]
    with patch("application.industry_classification._source", side_effect=AssertionError("live file read")):
        assert store.load_universe(day)["industry"].tolist() == ["电子", "电子"]
    with store.connect() as conn:
        payload = json.loads(conn.execute("SELECT payload FROM research_security_master_rows LIMIT 1").fetchone()[0])
    assert payload["industry_evidence"]["source_sha256"] == "frozen-sha"


def test_stale_master_and_unclassified_rows_fail_shared_live_gate():
    frame = pd.DataFrame({"symbol": ["600001", "600002"], "industry": ["", "未分类"]})
    frame.attrs["snapshot_date"] = "2026-08-28"
    quality = input_quality(frame, "2026-09-22")
    assert quality["master_age_days"] == 25
    assert not quality["master_ready"] and not quality["industry_ready"]
    assert not quality["ready"]


def test_master_repair_is_independent_of_market_completion():
    frame = pd.DataFrame({"symbol": ["600001"], "industry": [""]})
    frame.attrs["snapshot_date"] = "2026-08-28"
    store = Mock()
    store.load_universe.return_value = frame
    syncer = ResearchSynchronizer(store)
    with patch.object(syncer, "sync_master", return_value={"published": True}) as sync:
        result = syncer.repair_master_if_missing("2026-09-22")
    assert result["repaired"]
    sync.assert_called_once()
    store.completed_sync.assert_not_called()


def features():
    from test_independent_selector import _features
    frame = _features(50)
    frame["industry"] = [f"行业{i // 10}" for i in range(50)]
    return frame


def test_v2_valuation_compares_industry_peers_without_changing_weights():
    frame = features()
    for group in range(5):
        frame.loc[group * 10:(group + 1) * 10 - 1, "pe_ttm"] = [5 * (group + 1) + i for i in range(10)]
        frame.loc[group * 10:(group + 1) * 10 - 1, "pb"] = [group + 1 + i / 10 for i in range(10)]
    policy = IndependentPolicy(version="codex-independent-v2", industry_neutral=True,
                               max_per_industry=3, max_top5_per_industry=1)
    scored, _ = _score(frame, policy)
    indexed = scored.set_index("symbol")
    assert indexed.loc["600000", "valuation_score"] == indexed.loc["600040", "valuation_score"]
    assert sum(policy.public_dict()["component_weights"].values()) == 100
    frame["roe"] = frame["roe"].astype(float)
    frame.loc[0, "roe"] = float("inf")
    frame.loc[1, "industry"] = "未分类"
    scored, _ = _score(frame, policy)
    assert {"600000", "600001"}.isdisjoint(set(scored["symbol"]))
    legacy, _ = _score(frame, IndependentPolicy())
    assert {"600000", "600001"} <= set(legacy["symbol"])


def test_v2_applies_only_to_new_manifests_and_limits_each_industry():
    store = Mock()
    manifest = {"policy": {"industry_controls_version": "classified-v1"},
                "decision_context": {"selection_date": "2026-09-22", "decision_at": "2026-09-22T09:45:00+08:00"}}
    store.load_selection_manifest.return_value = manifest
    evidence = {"market_as_of": "2026-09-21", "market_input_hash": "h", "market_dataset_ids": [],
                "market_input_mode": "manifest_dataset_ids", "universe_count": 50, "hard_gate_count": 50}
    with patch("analysis.independent_selector._prepare_frame", return_value=(features(), evidence)):
        result = build("manifest", store=store)
        assert result["strategy_version"] == "codex-independent-v2"
        assert max(pd.Series([r["industry"] for r in result["top15"]]).value_counts()) == 3
        assert len({r["industry"] for r in result["top5"]}) == 5
        manifest["policy"] = {}
        legacy = build("manifest", store=store)
        assert legacy["strategy_version"] == "codex-independent-v1"
        assert legacy["top5"] == legacy["top15"][:5]


def test_old_policy_hash_inputs_do_not_gain_new_default_fields():
    assert "industry_controls_version" not in SelectionPolicy().as_dict()
    assert "max_top5_per_industry" not in FusionPolicy().as_dict()
    assert "industry_neutral" not in IndependentPolicy().public_dict()


def test_formal_top5_cap_applies_to_both_lane_floors_and_refill():
    from analysis.local_fusion import LocalFusionComposer
    rows = [{"symbol": f"600{i:03d}", "industry": industry,
             "assigned_lane": lane, "lane_score_raw": 100 - i}
            for i, (industry, lane) in enumerate([
                ("银行", "core"), ("银行", "core"), ("银行", "core"),
                ("银行", "satellite"), ("银行", "timing"),
                ("电子", "core"), ("医药", "core"), ("汽车", "core"),
            ])]
    composer = LocalFusionComposer(FusionPolicy(max_top5_per_industry=2))
    chosen = composer._shortlist_top5(rows, {})
    assert len(chosen) == 5
    assert sum(row["industry"] == "银行" for row in chosen) == 2
    # Scarcity must not relax the cap to fill a prettier list.
    assert len(composer._shortlist_top5(rows[:5], {})) == 2


def test_v2_small_industries_do_not_fall_back_to_global_valuation_ranks():
    frame = features()
    frame.loc[:3, "industry"] = "极小行业"
    policy = IndependentPolicy(version="codex-independent-v2", industry_neutral=True)
    scored, _ = _score(frame, policy)
    assert "极小行业" not in set(scored["industry"])


@pytest.mark.parametrize("detail,code", [
    ("master_quality=incomplete", "security_master_incomplete"),
    ("master_quality=ok industry_coverage=0.0%", "industry_coverage_insufficient"),
    ("partial: security_master_unavailable", "required_input_incomplete"),
])
def test_historical_success_logs_expose_partial_input_failures(detail, code):
    from application.scheduled_snapshot import _job_run
    job = _job_run({"job_name": "research_data_sync", "status": "success", "error": detail})
    assert job["status"] == "degraded"
    assert code in job["partial_failure_codes"]


@pytest.mark.parametrize("market_complete", [True, False])
def test_master_failure_cannot_be_masked_by_daily_sync_or_expected_valuation_delay(market_complete):
    from jobs import jobs_hub
    syncer = Mock()
    syncer.sync_master.return_value = {"quality_status": "incomplete", "industry_coverage": 0}
    syncer.sync_day.return_value = {
        "quality_status": "ok" if market_complete else "incomplete",
        "providers": {"zzshare": 5500}, "coverage": .99, "market_quality_status": "ok",
        "valuation_coverage": 1 if market_complete else 0, "valuation_quality_status": "unavailable",
    }
    syncer.store.update_selection_candidate_outcomes.return_value = {}
    with patch.object(jobs_hub, "_skip_if_not_trading", return_value=False), \
            patch("data.research_sync.ResearchSynchronizer", return_value=syncer), \
            patch("jobs.decision_loop_jobs.refresh_quality"), \
            patch.object(jobs_hub, "_log_run") as log:
        jobs_hub.task_research_data_sync()
    assert log.call_args.args[1] == "error"
    assert log.call_args.kwargs["error"].startswith("partial:")


def test_premarket_master_failure_stops_the_selection_context():
    from jobs import jobs_hub
    syncer = Mock()
    syncer.repair_master_if_missing.return_value = {"input_quality": {"ready": False}}
    with pytest.raises(RuntimeError, match="stage=security_master"):
        jobs_hub._preopen_research_context(syncer, "2026-09-22")


def test_api_and_scheduled_snapshot_expose_old_zero_industry_coverage():
    store = Mock()
    store.latest_formal_selection.return_value = {
        "run_id": "run", "selection_date": "2026-09-22", "status": "success",
        "metadata": {"industry_coverage": 0.0},
        "artifacts": {"formal_top15": {"payload": [{"symbol": "600000"}]},
                      "formal_top5": {"payload": [{"symbol": "600000"}]}},
    }
    result = SelectionRunService(store=store).latest_formal()
    assert result["status"] == "degraded"
    assert any("行业" in warning for warning in result["warnings"])
    snapshot = ScheduledSnapshotService._formal(result, {"latest_confirmed_open_date": "2026-09-22"})
    assert snapshot["status"] == "degraded"
    assert snapshot["input_quality"]["industry_coverage"] == 0.0
