import io
import json
import runpy
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from application import losna_research
from data import research_store


CLI = Path(__file__).resolve().parents[1] / "agent/skills/foliant-operator/scripts/foliant"
RESULT = {"reviews": [], "next_check": "复核"}


def test_cli_catalog_advertises_conditional_claim_replay(tmp_path):
    run = subprocess.run([sys.executable, str(CLI), "--config", str(tmp_path / "unused"), "catalog"],
                         capture_output=True, text=True, check=True)
    catalog = {item["name"]: item for item in json.loads(run.stdout)}
    claim = catalog["claim"]
    assert claim["annotations"]["idempotentHint"] is True
    assert "only with the same explicit claim_id" in claim["description"]
    assert "claim_id" in claim["inputSchema"]["properties"]
    assert not catalog["submit"].get("annotations", {}).get("idempotentHint")


@pytest.fixture
def operator(tmp_path, monkeypatch):
    store = research_store.ResearchStore(str(tmp_path / "research.db"), connect_fn=sqlite3.connect)
    clock = [datetime(2026, 11, 13, tzinfo=timezone.utc)]
    service = losna_research.LosnaResearch(store, clock=lambda: clock[0])
    service.create(purpose="directed_review", input_version="fixture",
                   candidates=[{"symbol": "600001"}], evidence=[{"evidence_id": "ref"}], owner="research")
    monkeypatch.setattr(research_store, "ResearchStore", lambda **kw: store)
    monkeypatch.setattr(losna_research, "LosnaResearch", lambda store: service)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"worker": "test", "owner": "research", "purposes": ["directed_review"]}))

    def call(name, data, *, expected_error=None, expected_identity=None):
        output = io.StringIO()
        monkeypatch.setattr(sys, "__stdout__", output)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
        monkeypatch.setattr(sys, "argv", [str(CLI), "--config", str(config), "call", name])
        if expected_error:
            with pytest.raises(SystemExit) as exit_info:
                runpy.run_path(str(CLI), run_name="__main__")
            assert exit_info.value.code == 1
            rejection = {"isError": True, "code": expected_error}
            if expected_identity is not None:
                rejection["object_id"] = expected_identity
            assert json.loads(output.getvalue()) == rejection
        else:
            runpy.run_path(str(CLI), run_name="__main__")
            return json.loads(output.getvalue())

    return call, clock, service


def test_research_cli_submit_rejects_with_domain_codes(operator):
    call, clock, _ = operator
    job = call("claim", {"claim_id": "first"})
    address = {"identity": job["object_id"]}
    original = {**address, "generation": job["revision"], "result": RESULT}
    review = {"symbol": "600001", "reason": "支持", "counterevidence": "待核", "evidence_refs": ["ref"]}
    for malformed, code in [
        (None, "research_result_invalid"),
        ({"unexpected": "private"}, "research_result_invalid"),
        ({**RESULT, "reviews": [review] * 4}, "research_budget_invalid"),
        ({**RESULT, "reviews": [None]}, "research_fields_invalid"),
        ({**RESULT, "reviews": [{**review, "symbol": []}]}, "research_subject_invalid"),
        ({**RESULT, "reviews": [{**review, "evidence_refs": ["private"]}]}, "research_evidence_invalid"),
        ({**RESULT, "reviews": [{**review, "counterevidence": ""}]}, "research_text_invalid"),
    ]:
        call("submit", {**original, "result": malformed}, expected_error=code)
    call("submit", {**original, "generation": job["revision"] - 1}, expected_error="research_generation_conflict")
    clock[0] += timedelta(hours=1)
    call("submit", original, expected_error="research_deadline_expired")
    assert call("status", address) == job


def test_research_cli_receipt_belongs_to_the_generation_that_submitted(operator):
    call, clock, _ = operator
    first = call("claim", {"claim_id": "first"})
    clock[0] += timedelta(minutes=4)
    second = call("claim", {"claim_id": "second"})
    address = {"identity": first["object_id"]}
    call("claim", {"claim_id": "first"}, expected_error="research_generation_conflict",
         expected_identity=first["object_id"])
    current = {**address, "result": RESULT, "generation": second["revision"]}
    receipt = call("submit", current)
    assert call("submit", current) == receipt == call("status", address)
    call("submit", {**current, "generation": first["revision"]}, expected_error="research_generation_conflict")
    call("submit", {**current, "result": {**RESULT, "next_check": "改动"}}, expected_error="research_result_conflict")


def test_research_cli_scanned_expiry_preserves_the_last_claim_generation(operator):
    call, clock, service = operator
    first = call("claim", {"claim_id": "first"})
    clock[0] += timedelta(minutes=4)
    last = call("claim", {"claim_id": "second"})
    clock[0] += timedelta(minutes=11)
    assert call("claim", {"claim_id": "scan"}) == {"status": "empty"}
    address = {"identity": last["object_id"]}
    expired = call("status", address)
    assert expired["status"] == "expired"
    request = {**address, "result": RESULT, "generation": last["revision"]}
    call("submit", request, expected_error="research_deadline_expired")
    for wrong in (first["revision"], expired["revision"]):
        call("submit", {**request, "generation": wrong}, expected_error="research_generation_conflict")
    with pytest.raises(losna_research.ResearchRejection, match="^research_not_found$"):
        service.submit(**request, owner="research", worker="other")
    assert call("status", address) == expired


def test_research_cli_denials_hide_out_of_scope_packets(operator):
    call, _, service = operator
    for purpose, owner, code in [
        ("portfolio_review", "private-owner", "research_not_found"),
        ("independent_discovery", "research", "purpose_denied"),
    ]:
        packet = service.create(purpose=purpose, input_version="fixture",
                                candidates=[], evidence=[{"evidence_id": "private"}], owner=owner)
        call("status", {"identity": packet["object_id"]}, expected_error=code)


@pytest.mark.parametrize("failure", [TimeoutError("private transport details"),
                                     ValueError("private storage details"), ValueError("research_result_invalid")])
def test_research_cli_unknown_after_commit_needs_status(operator, monkeypatch, capsys, failure):
    call, _, service = operator
    job = call("claim", {})
    submit = service.submit

    def lose_receipt(**kwargs):
        submit(**kwargs)
        raise failure

    monkeypatch.setattr(service, "submit", lose_receipt)
    address = {"identity": job["object_id"]}
    call("submit", {**address, "generation": job["revision"], "result": RESULT}, expected_error="outcome_unknown")
    assert "private" not in capsys.readouterr().err
    receipt = call("status", address)
    assert receipt["status"] == "received" and receipt["result"] == RESULT
