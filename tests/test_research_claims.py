"""Adversarial evidence and actual draft/lock/read workflows; no external I/O."""
from copy import deepcopy
from decimal import Decimal
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from application.research_cases import ResearchCases, compile_claim, validity
from application.research_claims import assess_claim
from data.research_store import ResearchStore
from webui.decision_loop_routes import register_decision_loop_routes


NOW = "2026-10-15T10:00:00+08:00"


def claim():
    return {
        "text": "收入增长20%，因此股价一定上涨",
        "passages": [{"source_id": "synthetic-filing", "locator": "p12/收入",
            "quote": "本期收入1,200，上期收入1,000（合成测试资料）",
            "published_at": "2026-10-14T08:00:00+08:00",
            "first_seen_at": "2026-10-14T01:00:00+00:00",
            "unit": "CNY_100M", "currency": "CNY", "basis": "consolidated", "period_kind": "annual"}],
        "calculation": {"operation": "growth_pct", "expected": "20", "operands": [
            {"passage_index": 0, "value": "1200"}, {"passage_index": 0, "value": "1000"}]},
        "expires_at": "2026-11-01T00:00:00+08:00",
    }


@pytest.fixture
def cases(tmp_path, monkeypatch):
    monkeypatch.setattr("application.research_cases.utcnow", lambda: NOW)
    return ResearchCases(ResearchStore(str(tmp_path / "claims.db"), connect_fn=sqlite3.connect))


def test_correct_arithmetic_does_not_verify_causality_or_source():
    value = compile_claim(**claim(), as_of=NOW)
    assert Decimal(value["computed_value"]) == 20
    assert value["state"] == "unverified"
    assert value["verification"]["arithmetic"] == "verified"
    assert value["verification"]["source_authenticity"] == "not_verified"
    assert value["verification"]["entity_period_alignment"] == "not_verified"
    summary = validity([value], now=NOW)
    assert summary["coverage"] == 0 and summary["arithmetic_coverage"] == 1
    assert summary["conclusion"] == "unverified" and summary["use"] == "review_required"


@pytest.mark.parametrize(("quote", "operand", "result"), [
    ("金额 1,200", "1200", "1200"), ("金额 1，200.50", "1,200.5", "1200.5"),
    ("金额 −100.25", "-100.25", "-100.25"), ("金额 －100", "-100", "-100"),
    ("金额（1,200）", "-1200", "-1200"), ("金额 ($1,200)", "-1200", "-1200"),
    ("金额 +100.00", "100", "100"), ("比率 .25", "0.25", "0.25"),
])
def test_complete_number_extraction(quote, operand, result):
    raw = claim()
    raw["passages"][0]["quote"] = quote
    raw["calculation"] = {"operation": "sum", "operands": [{"passage_index": 0, "value": operand}]}
    assert Decimal(compile_claim(**raw, as_of=NOW)["computed_value"]) == Decimal(result)


@pytest.mark.parametrize(("quote", "operand"), [
    ("金额 1,200", "200"), ("金额 1,200", "1"), ("金额 1，200", "200"),
    ("金额 -100", "100"), ("金额 －100", "100"), ("金额 (100)", "100"),
    ("金额 1,20", "20"), ("金额 1e3", "3"), ("金额 1e3", "1"),
    ("编号 0x100", "100"), ("金额 1200.5", "200"), ("金额 １２3", "3"),
])
def test_numeric_suffixes_and_sign_loss_are_rejected(quote, operand):
    raw = claim()
    raw["passages"][0]["quote"] = quote
    raw["calculation"] = {"operation": "sum", "operands": [{"passage_index": 0, "value": operand}]}
    with pytest.raises(ValueError, match="operand_not_in_passage"):
        compile_claim(**raw, as_of=NOW)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "1e3", "9" * 100, None])
def test_unbounded_and_nonfinite_numeric_inputs_are_rejected(value):
    raw = claim()
    raw["calculation"]["operands"][0]["value"] = value
    with pytest.raises(ValueError, match="numeric_literal"):
        compile_claim(**raw, as_of=NOW)


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("first_seen_at", "2026-10-15T02:00:00.000001+00:00", "post_decision_evidence_forbidden"),
    ("published_at", "2026-10-14T10:00:00+08:00", "passage_observed_before_publication"),
    ("first_seen_at", "2026-10-14T01:00:00", "timezone_required"),
    ("published_at", "not-a-date", "timestamp_invalid"),
])
def test_temporal_constraints(field, value, reason):
    raw = claim()
    raw["passages"][0][field] = value
    with pytest.raises(ValueError, match=reason):
        compile_claim(**raw, as_of=NOW)


def test_equal_instants_across_offsets_and_expiry_are_distinct():
    raw = claim()
    raw["passages"][0].update(published_at="2026-10-15T10:00:00+08:00", first_seen_at="2026-10-15T02:00:00Z")
    raw["expires_at"] = "2026-10-15T10:00:00+08:00"
    compiled = compile_claim(**raw, as_of=NOW)
    check = assess_claim(compiled, now=NOW)
    assert check["temporal"] == "eligible" and check["freshness"] == "stale"
    assert "claim_expired_or_expiry_invalid" in check["reasons"]


def test_declared_semantics_and_zero_denominator_remain_guarded():
    raw = claim()
    raw["passages"].append({**raw["passages"][0], "currency": "USD"})
    raw["calculation"]["operands"][1]["passage_index"] = 1
    with pytest.raises(ValueError, match="incompatible_calculation_semantics"):
        compile_claim(**raw, as_of=NOW)
    raw = claim()
    raw["passages"][0]["quote"] = "本期1200，上期0"
    raw["calculation"]["operands"][1]["value"] = "0"
    with pytest.raises(ValueError, match="unsupported_calculation"):
        compile_claim(**raw, as_of=NOW)


def test_missing_provenance_and_empty_calculation_cannot_silently_pass():
    raw = claim()
    raw["passages"][0]["source_id"] = " "
    with pytest.raises(ValueError, match="passage_provenance_required"):
        compile_claim(**raw, as_of=NOW)
    raw = claim()
    raw["calculation"] = {}
    with pytest.raises(ValueError, match="bounded_operands_required"):
        compile_claim(**raw, as_of=NOW)


def test_missing_cutoff_and_malformed_legacy_claims_remain_readable():
    old = claim()
    old.update(state="supported", computed_value="999", verification={"entailment": "supported"})
    original = deepcopy(old)
    checks = validity([old, {"state": "supported"}, None], now=NOW)
    assert checks["conclusion"] == "unverified" and checks["coverage"] == 0
    assert checks["claim_checks"][0]["temporal"] == "unknown"
    assert "evidence_cutoff_unknown" in checks["claim_checks"][0]["reasons"]
    assert Decimal(checks["claim_checks"][0]["computed_value"]) == 20
    assert old == original


def test_draft_lock_read_retains_cutoff_owner_and_immutable_history(cases, monkeypatch):
    raw = claim()
    raw.update(state="supported", verification={"entailment": "supported"}, evidence_as_of="2099-01-01T00:00:00Z")
    draft = cases.draft("600001", owner="alice", text="观察", claims=[raw])
    assert draft["evidence_as_of"] == NOW
    assert draft["claims"][0]["evidence_as_of"] == "2026-10-15T02:00:00+00:00"
    assert draft["validity"]["conclusion"] == "unverified"
    monkeypatch.setattr("application.research_cases.utcnow", lambda: "2026-10-16T10:00:00+08:00")
    locked = cases.lock("600001", owner="alice", draft_revision=1, human_confirmed=True)
    assert locked == cases.lock("600001", owner="alice", draft_revision=1, human_confirmed=True)
    assert locked["evidence_as_of"] == NOW
    cases.draft("600001", owner="alice", text="新草稿", claims=[], expected_revision=1)
    view = cases.view(owner="alice", now="2026-10-16T10:00:00+08:00")
    assert view["theses"][0]["text"] == "观察"
    assert view["theses"][0]["validity"]["claim_checks"][0]["as_of"] == "2026-10-15T02:00:00+00:00"
    assert not cases.view(owner="bob", now=NOW)["theses"]
    assert not cases.view(now=NOW)["drafts"]
    assert cases.repo.get("thesis_lock", "600001", "alice")["claims"] == locked["claims"]


def test_legacy_supported_record_is_rechecked_without_rewriting(cases):
    raw = claim()
    raw.update(state="supported", computed_value="20", semantic_entailment="not_automatically_proven")
    raw["passages"][0]["first_seen_at"] = "2026-10-16T10:00:00+08:00"
    stored = cases.repo.put("thesis_lock", "600001", {
        "claims": [raw], "text": "历史论点", "locked_at": NOW, "evidence_as_of": NOW,
        "next_check": "2026-11-01T00:00:00Z",
    }, owner="alice")
    view = cases.view(owner="alice", now="2026-10-20T10:00:00+08:00")
    check = view["theses"][0]["validity"]["claim_checks"][0]
    assert check["temporal"] == "invalid" and check["arithmetic"] == "not_verified"
    assert "post_decision_evidence_forbidden" in check["reasons"]
    assert cases.repo.get("thesis_lock", "600001", "alice") == stored


def test_legacy_lock_time_does_not_invent_a_draft_cutoff(cases):
    raw = claim()
    raw["state"] = "supported"
    cases.repo.put("thesis_lock", "600001", {"claims": [raw], "text": "旧版锁定", "locked_at": NOW}, owner="alice")
    view = cases.view(owner="alice", now=NOW)
    check = view["theses"][0]["validity"]["claim_checks"][0]
    assert check["temporal"] == "unknown"
    assert "evidence_cutoff_unknown" in check["reasons"]


def test_http_draft_lock_query_and_failed_update_are_one_contract(cases, monkeypatch):
    monkeypatch.setattr("data.research_store.ResearchStore", lambda *a, **k: cases.repo.store)
    app = FastAPI()
    register_decision_loop_routes(app, agent_result=lambda r, **k: r,
                                 agent_error=lambda e: {"error": type(e).__name__}, browser_ok=lambda r: r)
    with TestClient(app) as client:
        payload = {"symbol": "600001", "text": "待核实", "claims": [claim()]}
        draft = client.post("/api/research/thesis/draft", json=payload)
        assert draft.status_code == 200
        assert draft.json()["validity"]["claim_checks"][0]["arithmetic"] == "verified"
        lock_payload = {"symbol": "600001", "draft_revision": 1, "confirm": True}
        lock = client.post("/api/research/thesis/lock", json=lock_payload)
        assert lock.status_code == 200 and lock.json()["validity"]["conclusion"] == "unverified"
        assert client.post("/api/research/thesis/lock", json=lock_payload).json() == lock.json()
        private = client.get("/api/research/cases").json()
        assert private["theses"][0]["validity"] == draft.json()["validity"]
        assert not client.get("/api/machine/v1/agent/research/cases").json()["data"]["theses"]
        payload["claims"][0]["passages"][0]["first_seen_at"] = "2026-10-16T00:00:00Z"
        payload["expected_revision"] = 1
        rejected = client.post("/api/research/thesis/draft", json=payload)
        assert rejected.status_code == 422
        assert rejected.json()["detail"] == "post_decision_evidence_forbidden"
        assert cases.repo.get("thesis_draft", "600001", "portfolio-primary")["revision"] == 1
