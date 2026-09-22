from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from application.external_research import (
    CHANNEL,
    ExternalIndependentResearchService,
)
from application.services import ApplicationError
from data.research_store import ResearchStore
from webui.external_research_routes import (
    ExternalIndependentBundleReq, register_external_research_routes,
)


NOW = datetime(2026, 9, 15, 13, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
RUN_ID = "formal-run-external-overlay"
BASE_SYMBOLS = [f"600{i:03d}" for i in range(1, 16)]
ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path):
    path = tmp_path / "research.db"
    return ResearchStore(
        str(path), connect_fn=sqlite3.connect, is_postgres=False, ensure_schema=True,
    )


def _seed_formal(store, *, strategy_version="codex-independent-v1"):
    metadata = {
        "snapshot_id": "independent-input-snapshot",
        "policy_hash": "formal-policy-hash",
        "rule_version": "formal-rule-v1",
    }
    conn = store.connect()
    try:
        conn.execute(
            """INSERT INTO selection_runs
               (run_id,selection_date,created_at,status,primary_source,universe_count,
                eligible_count,final_count,coverage,reference_source,comparison,metadata,
                publication_status,published_at,supersedes_run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (RUN_ID, "2026-09-14", NOW.isoformat(), "success", "local", 100, 30,
             15, 1.0, "wencai", "{}", json.dumps(metadata), "published",
             NOW.isoformat(), None),
        )
        conn.execute(
            """INSERT INTO selection_input_manifests
               (manifest_id,run_id,decision_context,universe_snapshot_id,
                market_dataset_ids,valuation_dataset_ids,financial_revision_set_id,
                event_dataset_id,policy_version,policy_hash,policy_payload,code_revision,
                dependency_lock_hash,strategy_snapshot,publication_generations,
                schema_version,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("manifest-external", RUN_ID, "{}", "universe-1", "[]", "[]",
             "financial-1", "event-1", "v1", "formal-policy-hash", "{}",
             "revision-1", None, None, None, "selection-input-manifest-v1",
             NOW.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    formal_rows = [
        {"symbol": f"000{i:03d}", "name": f"正式{i}", "rank": i}
        for i in range(1, 16)
    ]
    store.save_selection_artifact(RUN_ID, "formal_top15", formal_rows)
    store.save_selection_artifact(RUN_ID, "formal_top5", formal_rows[:5])
    independent_rows = [
        {
            "symbol": symbol, "name": f"独立{i}", "rank": i,
            "total_score": 101 - i,
        }
        for i, symbol in enumerate(BASE_SYMBOLS, 1)
    ]
    store.save_selection_artifact(RUN_ID, "independent_selection", {
        "status": "ready",
        "strategy_id": "codex-independent",
        "strategy_version": strategy_version,
        "strategy_hash": "independent-strategy-hash",
        "input_snapshot_id": "independent-input-snapshot",
        "market_as_of": "2026-09-14",
        "top15": independent_rows,
        "top5": independent_rows[:5],
    })


def _bundle(*, decision=NOW, with_evidence=True, proposal=True):
    evidence = [{
        "source_url": "https://example.test/announcement/2026-001",
        "source_type": "announcement",
        "published_at": (decision - timedelta(minutes=10)).isoformat(),
        "event_at": (decision - timedelta(minutes=10)).isoformat(),
        "captured_at": (decision - timedelta(minutes=5)).isoformat(),
        "symbols": [BASE_SYMBOLS[0], BASE_SYMBOLS[1]],
        "industries": ["示例行业"],
        "direction": 1,
        "confidence": 0.9,
        "expiry": (decision + timedelta(days=2)).isoformat(),
        "dedupe_key": "announcement:2026-001",
        "primary_source_confirmed": True,
        "controversy_status": "confirmed",
    }] if with_evidence else []
    overlay = [
        {
            "symbol": symbol,
            "event_adjustment": 8 if i == 1 else (-15 if i == 2 else 0),
            "risk_veto": i == 2,
            "evidence_ids": ["announcement:2026-001"] if with_evidence and i <= 2 else [],
        }
        for i, symbol in enumerate(BASE_SYMBOLS, 1)
    ]
    proposals = [{
        "feature": "event_adjustment",
        "mature_sample_count": 999,
        "covered_weeks": 999,
        "out_of_sample_delta": 0.8,
        "expected_impact": "提高独立观察排序稳定性",
        "acceptance_metric": "时间切分样本外收益改善",
        "rollback_condition": "样本外收益转负",
        "time_split_validated": True,
        "status": "proposed",
    }] if proposal else []
    return {
        "channel": CHANNEL,
        "idempotency_key": f"ext-{decision.strftime('%Y%m%d%H%M%S')}",
        "selection_run_id": RUN_ID,
        "decision_as_of": decision.isoformat(),
        "market_regime": "sideways",
        "external_evidence": evidence,
        "independent_overlay": overlay,
        "news_watchlist": [{
            "symbol": "300001", "name": "新闻观察", "industry": "观察行业",
            "reason": "公开新闻发现但量化字段不完整",
            "evidence_ids": ["announcement:2026-001"] if with_evidence else [],
        }],
        "tuning_proposals": proposals,
    }


def _service(tmp_path, *, strategy_version="codex-independent-v1"):
    store = _store(tmp_path)
    _seed_formal(store, strategy_version=strategy_version)
    return store, ExternalIndependentResearchService(store=store, clock=lambda: NOW)


@pytest.mark.parametrize("strategy_version", ["codex-independent-v1", "codex-independent-v2"])
def test_contract_saves_bounded_overlay_without_mutating_formal_artifacts(tmp_path, strategy_version):
    store, service = _service(tmp_path, strategy_version=strategy_version)
    before = store.formal_selection(RUN_ID)
    before_hashes = {
        name: value["payload_hash"] for name, value in before["artifacts"].items()
    }

    bundle = _bundle()
    watch_evidence = dict(bundle["external_evidence"][0])
    watch_evidence.update({
        "source_url": "https://example.test/news/watch-only",
        "source_type": "news",
        "symbols": ["300001"],
        "dedupe_key": "news:watch-only",
    })
    bundle["external_evidence"].append(watch_evidence)
    bundle["news_watchlist"][0]["evidence_ids"] = ["news:watch-only"]
    result = service.save(bundle, actor_id="research-agent")
    data = result["data"]
    latest = service.latest_data()

    assert result["status"] == "complete"
    assert data["overlay"]["base_strategy_version"] == strategy_version
    assert data["overlay"]["idempotency_key"] == bundle["idempotency_key"]
    assert {row["symbol"] for row in data["overlay"]["top15"]} == set(BASE_SYMBOLS)
    assert data["overlay"]["top5"][0]["symbol"] == BASE_SYMBOLS[0]
    assert data["overlay"]["top15"][-1]["risk_veto"] is True
    assert min(row["event_adjustment"] for row in data["overlay"]["top15"]) == -15
    assert max(row["event_adjustment"] for row in data["overlay"]["top15"]) == 8
    assert data["news_watchlist"][0]["status"] == "observation_only"
    assert data["news_watchlist"][0]["formal_top15_eligible"] is False
    assert data["tuning_proposals"][0]["mature_sample_count"] == 0
    assert data["tuning_proposals"][0]["status"] == "evidence_insufficient"
    assert data["tuning_proposals"][0]["auto_apply"] is False
    assert latest["auto_execution"] is False
    assert {row["dedupe_key"] for row in latest["evidence"]} == {
        "announcement:2026-001", "news:watch-only",
    }
    assert latest["overlay"]["price_authority"].startswith("none")
    serialized = json.dumps(latest, ensure_ascii=False)
    assert '"execution_price"' not in serialized
    assert '"target_price"' not in serialized
    assert '"stop_price"' not in serialized

    after = store.formal_selection(RUN_ID)
    assert {
        name: value["payload_hash"] for name, value in after["artifacts"].items()
    } == before_hashes


def test_submission_is_idempotent_and_notification_is_claimed_once_per_planned_slot(tmp_path):
    store, service = _service(tmp_path)
    bundle = _bundle(proposal=False)
    first = service.save(bundle, actor_id="research-agent")
    overlay_id = first["data"]["overlay"]["overlay_id"]

    later = ExternalIndependentResearchService(
        store=store, clock=lambda: NOW + timedelta(hours=1),
    )
    replay = later.save(bundle, actor_id="research-agent")
    assert replay["data"]["idempotency"] == {
        "key": bundle["idempotency_key"], "replayed": True,
        "notification_status": "pending",
    }
    for planned_time in ("10:15", "11:25", "14:35", "20:45"):
        slot = f"2026-09-15T{planned_time}+08:00"
        first_claim = service.claim_notification(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot=slot, actor_id="research-agent",
        )
        recorded = service.record_notification_delivery(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot=slot, sent=True, error_code=None,
            actor_id="research-agent",
        )
        second_claim = service.claim_notification(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot=slot, actor_id="research-agent",
        )
        assert first_claim["should_send"] is True
        assert first_claim["notification_slot"] == slot
        assert recorded["delivery_status"] == "delivered"
        assert second_claim["should_send"] is False
        assert second_claim["status"] == "delivery_replayed"
        assert second_claim["prior_sent"] is True
        assert second_claim["sent"] is True
    with pytest.raises(PermissionError, match="external_submission_actor_mismatch"):
        service.claim_notification(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot="2026-09-16T10:15+08:00",
            actor_id="other-research-agent",
        )
    with pytest.raises(ValueError, match="external_notification_slot_invalid"):
        service.claim_notification(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot="2026-09-15T12:00+08:00", actor_id="research-agent",
        )
    with pytest.raises(ValueError, match="external_notification_slot_date_mismatch"):
        service.claim_notification(
            idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
            notification_slot="2026-09-16T10:15+08:00", actor_id="research-agent",
        )

    changed = dict(bundle)
    changed["market_regime"] = "bear"
    with pytest.raises(ValueError, match="external_idempotency_key_conflict"):
        later.save(changed, actor_id="research-agent")

    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM external_research_submissions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM external_research_notification_claims"
        ).fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM external_independent_overlays").fetchone()[0] == 1
    finally:
        conn.close()


def test_notification_replay_distinguishes_unfinished_and_failed_delivery(tmp_path):
    store, service = _service(tmp_path)
    bundle = _bundle(proposal=False)
    overlay_id = service.save(bundle, actor_id="research-agent")["data"]["overlay"]["overlay_id"]
    unfinished_slot = "2026-09-15T10:15+08:00"
    service.claim_notification(
        idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
        notification_slot=unfinished_slot, actor_id="research-agent",
    )
    unfinished = service.claim_notification(
        idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
        notification_slot=unfinished_slot, actor_id="research-agent",
    )
    assert unfinished["status"] == "delivery_pending"
    assert unfinished["prior_sent"] is False
    assert unfinished["delivery_status"] == "claimed"

    failed_slot = "2026-09-15T11:25+08:00"
    service.claim_notification(
        idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
        notification_slot=failed_slot, actor_id="research-agent",
    )
    service.record_notification_delivery(
        idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
        notification_slot=failed_slot, sent=False, error_code="qq_delivery_failed",
        actor_id="research-agent",
    )
    failed = service.claim_notification(
        idempotency_key=bundle["idempotency_key"], overlay_id=overlay_id,
        notification_slot=failed_slot, actor_id="research-agent",
    )
    assert failed["status"] == "delivery_replayed"
    assert failed["prior_sent"] is False
    assert failed["delivery_status"] == "failed"
    assert failed["delivery_error_code"] == "qq_delivery_failed"


def test_pit_rejects_historical_rank_and_post_decision_evidence_atomically(tmp_path):
    store, service = _service(tmp_path)
    historical = _bundle(decision=NOW - timedelta(minutes=16))
    with pytest.raises(ValueError, match="historical_ranking_backfill_forbidden"):
        service.save(historical, actor_id="research-agent")

    future_evidence = _bundle()
    future_evidence["external_evidence"][0]["captured_at"] = (
        NOW + timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ValueError, match="post_decision_evidence_forbidden"):
        service.save(future_evidence, actor_id="research-agent")

    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM external_independent_overlays").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM external_research_evidence").fetchone()[0] == 0
    finally:
        conn.close()


def test_source_isolation_rejects_external_membership_and_bad_veto(tmp_path):
    _store_value, service = _service(tmp_path)
    injected = _bundle()
    injected["independent_overlay"][0]["symbol"] = "300001"
    with pytest.raises(ValueError, match="overlay_membership_must_equal_independent_base"):
        service.save(injected, actor_id="research-agent")

    bad_veto = _bundle(with_evidence=False)
    bad_veto["independent_overlay"][0]["risk_veto"] = True
    with pytest.raises(ValueError, match="overlay_adjustment_requires_evidence"):
        service.save(bad_veto, actor_id="research-agent")

    out_of_bounds = _bundle()
    out_of_bounds["independent_overlay"][0]["event_adjustment"] = 8.01
    with pytest.raises(ValueError, match="event_adjustment_out_of_bounds"):
        service.save(out_of_bounds, actor_id="research-agent")


def test_evidence_dedupe_detects_conflicting_revisions(tmp_path):
    store, service = _service(tmp_path)
    first = service.save(_bundle(proposal=False), actor_id="research-agent")
    evidence_id = first["data"]["evidence"][0]["evidence_id"]
    later = NOW + timedelta(minutes=1)
    changed = _bundle(decision=later, proposal=False)
    changed["external_evidence"][0]["confidence"] = 0.5
    changed["independent_overlay"][0]["evidence_ids"] = [evidence_id]
    changed["independent_overlay"][1]["evidence_ids"] = [evidence_id]
    changed["news_watchlist"][0]["evidence_ids"] = [evidence_id]
    later_service = ExternalIndependentResearchService(store=store, clock=lambda: later)
    with pytest.raises(ValueError, match="external_evidence_dedupe_conflict"):
        later_service.save(changed, actor_id="research-agent")


def test_route_exposes_safe_dedupe_conflict_code(monkeypatch):
    app = FastAPI()
    register_external_research_routes(
        app, agent_result=lambda value, **_kwargs: value,
        agent_error=lambda error: error,
    )
    route = next(route for route in app.routes
                 if getattr(route, "path", None) ==
                 "/api/machine/v1/agent/external-independent-research"
                 and "POST" in (getattr(route, "methods", None) or set()))
    monkeypatch.setattr(
        ExternalIndependentResearchService, "save",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("external_evidence_dedupe_conflict")
        ),
    )
    request = SimpleNamespace(state=SimpleNamespace(
        agent_identity=SimpleNamespace(agent_id="research-agent"),
    ))
    error = route.endpoint(ExternalIndependentBundleReq.model_validate(_bundle()), request)
    assert isinstance(error, ApplicationError)
    assert error.code == "external_evidence_dedupe_conflict"
    assert error.status_code == 409
    assert "versioned key" in error.message


def test_outcomes_cover_all_horizons_and_market_regime(tmp_path):
    store, service = _service(tmp_path)
    service.save(_bundle(proposal=False), actor_id="research-agent")
    dates = []
    day = datetime(2026, 9, 14)
    while len(dates) < 21:
        if day.weekday() < 5:
            dates.append(day.date().isoformat())
        day += timedelta(days=1)
    conn = store.connect()
    try:
        for symbol in BASE_SYMBOLS:
            for index, trade_date in enumerate(dates):
                conn.execute(
                    """INSERT INTO research_daily_bars
                       (symbol,trade_date,adjustment,open,high,low,close,volume,amount,
                        turnover_rate,is_paused,is_st,provider,origin,effective_at,retrieved_at,
                        unit,schema_version,quality_status,dataset_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, trade_date, "qfq", 10 + index, 11 + index, 9 + index,
                     10 + index, 1000, 10000, 1, 0, 0, "test", "test",
                     trade_date, NOW.isoformat(), "yuan", "v1", "ok", "dataset-1"),
                )
        conn.commit()
    finally:
        conn.close()

    settled = service.settle_outcomes()
    assert settled == {"inserted": 75, "pending": 0}
    outcomes = service.latest_data()["outcomes"]
    assert outcomes["horizons"] == [1, 3, 5, 10, 20]
    assert {row["horizon_days"] for row in outcomes["buckets"]} == {1, 3, 5, 10, 20}
    assert {row["market_regime"] for row in outcomes["buckets"]} == {"sideways"}


def test_tuning_gate_uses_persisted_samples_then_supports_rollback(tmp_path):
    store, service = _service(tmp_path)
    conn = store.connect()
    try:
        week_starts = ("2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07")
        for index in range(20):
            decision = f"{week_starts[index // 5]}T13:30:00+08:00"
            conn.execute(
                """INSERT INTO external_overlay_outcomes
                   (overlay_id,symbol,horizon_days,decision_as_of,market_regime,
                    base_score,event_adjustment,risk_veto,return_pct,outcome_status,evaluated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (f"mature-overlay-{index:02d}", f"{600100 + index:06d}", 5, decision,
                 "sideways", 80 + index, 1 if index % 2 == 0 else 0, 0,
                 1.2 if index % 2 == 0 else 0.2, "matured", NOW.isoformat()),
            )
        conn.commit()
    finally:
        conn.close()

    bundle = _bundle(with_evidence=False)
    bundle["news_watchlist"] = []
    for row in bundle["independent_overlay"]:
        row["event_adjustment"] = 0
        row["risk_veto"] = False
    result = service.save(bundle, actor_id="research-agent")
    proposal = result["data"]["tuning_proposals"][0]
    assert proposal["mature_sample_count"] == 20
    assert proposal["covered_weeks"] == 4
    assert proposal["status"] == "review_required"
    assert proposal["out_of_sample_delta"] == 1.0
    assert proposal["auto_apply"] is False

    rollback = service.rollback_proposal(proposal["proposal_id"])
    assert rollback == {
        "proposal_id": proposal["proposal_id"], "status": "rolled_back",
        "applied_policy_hash": None, "auto_apply": False,
    }
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT validation_status,applied_policy_hash FROM strategy_adjustment_proposals "
            "WHERE proposal_id=?", (proposal["proposal_id"],),
        ).fetchone()
        assert row == ("rolled_back", None)
    finally:
        conn.close()


def test_machine_contract_is_strict_and_requires_research_preview_capability():
    access_source = (ROOT / "webui" / "access_control.py").read_text()
    route_source = (ROOT / "webui" / "external_research_routes.py").read_text()
    assert access_source.count(
        '("GET", "/api/machine/v1/agent/external-independent-research")'
    ) == 2
    assert access_source.count(
        '("POST", "/api/machine/v1/agent/external-independent-research")'
    ) == 2
    assert access_source.count(
        '("POST", "/api/machine/v1/agent/external-independent-research/notification-claim")'
    ) == 2
    assert access_source.count(
        '("POST", "/api/machine/v1/agent/external-independent-research/notification-delivery")'
    ) == 2
    assert '"foliant.selection.read"' in access_source
    assert '"foliant.selection.preview"' in access_source
    assert "ConfigDict(extra=\"forbid\"" in route_source

    invalid = _bundle()
    invalid["private_browser_history"] = ["forbidden"]
    with pytest.raises(ValidationError):
        ExternalIndependentBundleReq(**invalid)
