"""Opt-in native PostgreSQL contract test; all writes stay in a unique test schema."""
import os
from pathlib import Path
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="PostgreSQL integration is opt-in")
def test_native_migration_cas_model_ledger_and_holdout():
    import _bootstrap  # noqa: F401 - match application module-path initialization
    from core.db_compat import _PGConnection
    from psycopg2 import sql
    from data.research_store import ResearchStore
    from analysis.local_fusion import FusionPolicy
    from analysis.decision_evaluation import ExecutionRules
    from application.decision_capsule import build_capsule
    from application.decision_loop import DecisionLoopService
    from application.model_portfolios import ModelPortfolios
    schema = "test_decision_loop_" + uuid.uuid4().hex
    admin = _PGConnection()
    admin._conn.cursor().execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    admin.commit()

    def connect(_name=None):
        conn = _PGConnection()
        conn._conn.cursor().execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        return conn

    try:
        store = ResearchStore(connect_fn=connect, is_postgres=True)
        conn = store.connect()
        migration = (Path(__file__).resolve().parents[1] / "scripts/migrations/11-research-decision-loop.sql").read_text()
        conn.execute(migration)
        conn.execute(migration)  # fresh and repeat execution are both safe
        reliability_migration = (Path(__file__).resolve().parents[1] / "scripts/migrations/12-research-reliability.sql").read_text()
        conn.execute(reliability_migration)
        conn.execute(reliability_migration)
        conn.commit()
        conn.close()
        from data.reliability_store import ReliabilityStore
        journal = ReliabilityStore(store)
        journal.put("thesis_draft", "600001", {"text": "private"}, owner="fixture-owner")
        assert journal.get("thesis_draft", "600001", "other") is None
        def revise(number):
            try:
                journal.put("thesis_draft", "600001", {"text": str(number)}, owner="fixture-owner", expected_revision=1)
                return "updated"
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(revise, [1, 2])) == ["conflict", "updated"]
        for number in range(4):
            journal.enqueue("fixture", str(number), {"number": number})
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: journal.claim("fixture", limit=2), [1, 2]))
        assert len({r["work_id"] for claim in claims for r in claim}) == 4
        policy = FusionPolicy().as_dict()
        store.save_selection_strategy_records("r1", [], policy=policy, policy_hash="base", selection_date="2026-09-04")

        def propose(number):
            try:
                store.save_strategy_policy_proposal(
                    {"proposal_id": "proposal" + str(number), "base_policy_hash": "base",
                     "evidence_snapshot_id": "fixture", "effective_from": "2099-01-01T09:00:00+08:00"},
                    validation_status="applied", applied_policy={**policy, "top15_satellite_cap": number + 2})
                return "applied"
            except ValueError as exc:
                assert "compare_and_swap" in str(exc)
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(propose, (1, 2))) == ["applied", "conflict"]
        assert store.load_active_strategy_policy()["policy_hash"] == "base"

        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        execution_day = (now + timedelta(days=3)).date().isoformat()
        capsule = build_capsule(run_id="r1", metadata={"selection_date": now.date().isoformat(), "policy_hash": "base"},
                                top15=[{"symbol": "600001", "assigned_lane": "core"}], top5=[{"symbol": "600001"}],
                                published_at=(now - timedelta(seconds=5)).isoformat(), next_open_date=execution_day)
        service = DecisionLoopService(store)
        assert service.start_model_cohorts(capsule)["created"] > 0
        assert service.start_model_cohorts(capsule)["created"] == 0
        facts = {("600001", execution_day): {"trade_date": execution_day, "open": 10, "close": 10.2,
                "volume": 1000000, "limit_up": 11, "limit_down": 9, "adjustment": "raw",
                "execution_rules": asdict(ExecutionRules()), "corporate_actions_complete": True}}
        evaluated_at = execution_day + "T18:00:00+08:00"
        assert service.settle_models(facts, now=evaluated_at)
        assert not service.settle_models(facts, now=evaluated_at)
        books = ModelPortfolios(store).advance(facts, now=evaluated_at)
        assert books["fusion"]["net_asset_value"] == books["top5"]["net_asset_value"]
        assert books["fusion"]["status"] == "verified"
        assert ModelPortfolios(store).advance(facts, now=evaluated_at) == {}
        trial = service.register_trial(hypothesis_id="trend_exhaustion", ast={"op": "field", "name": "close"},
                                       dataset_id="fixture", data_class="exploratory", code_revision="fixture")
        assert not service.finish_trial(trial["trial_id"])["evidence"]["promotion_ready"]
        conn = store.connect()
        conn.execute("INSERT INTO research_holdout_batches VALUES ('interrupted','2026-01-01','2026-02-01','evaluating',?,NULL)",
                     (trial["trial_id"],))
        conn.commit()
        conn.close()
        audited = service.void_holdout("interrupted", trial["trial_id"], reason="fixture worker interrupted")
        assert not audited["promotion_ready"]
        assert service.void_holdout("interrupted", trial["trial_id"], reason="idempotent") == audited
        start = (now + timedelta(days=30)).date().isoformat()
        end = (now + timedelta(days=60)).date().isoformat()
        batch = service.seal_holdout(start_date=start, end_date=end)
        with pytest.raises(ValueError, match="not_matured"):
            service.consume_holdout(batch["batch_id"], trial["trial_id"])
    finally:
        # This schema was created by this test and its generated identifier is
        # never sourced from user configuration or an existing database object.
        admin._conn.cursor().execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.commit()
        admin.close()
