from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from analysis.decision_evaluation import ExecutionRules
from application.weekly_contribution import weekly_contribution
from application.weekly_paper_pair import evaluate_pair, freeze_pair
from data.reliability_store import ReliabilityStore
from data.research_store import ResearchStore
from webui.decision_loop_routes import register_decision_loop_routes

DAY = "2026-10-12"
START = datetime.fromisoformat(DAY + "T17:20:00+08:00")
CLOSE = datetime.fromisoformat("2026-10-13T16:30:00+08:00")
LATER = datetime.fromisoformat("2026-10-21T16:00:00+08:00")
A, B, C = "600001", "600002", "600003"


@pytest.fixture
def store(tmp_path):
    return ResearchStore(str(tmp_path / "weekly.db"), connect_fn=sqlite3.connect)


def publish(store, name):
    capsule = {"run_id": name, "capsule_id": "cap_"+name, "manifest_id": "manifest_"+name,
               "recording_mode": "contemporaneous", "selection_date": DAY,
               "published_at": DAY+"T09:00:00+08:00", "earliest_execution_at": "2026-10-13T09:30:00+08:00",
               "opportunity_set":{"top15":[{"symbol":B},{"symbol":C}]}}
    conn = store.connect()
    conn.execute("INSERT INTO selection_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (name, DAY, DAY, "success", "fixture", 3, 3, 2, 1., None, "{}", "{}", "published", capsule["published_at"], None))
    store._insert_selection_artifact(conn, name, "decision_capsule", capsule, {})
    conn.commit()
    conn.close()
    return capsule


def packet(store, name, *, reviews=True, status="received", identity=None, owner="research", purpose="independent_discovery"):
    identity = identity or "p_"+name
    fact = {"symbol":A, "pe_ttm":6}
    data = {"packet":{"run_id":name,"purpose":purpose,"deadline":DAY+"T10:15:00+08:00",
        "candidates":[{"symbol":A},{"symbol":B}], "evidence":[{"evidence_id":"claim","fact":fact},
            {"evidence_id":"scope","fact":{"manifest_id":"manifest_"+name,"prefilter":"left"}}]},
        "status":status, "received_at":DAY+"T10:01:00+08:00",
        "result":{"reviews":[{"symbol":A,"reason":"合成理由","counterevidence":"待核负债","evidence_refs":["claim"]}] if reviews else [],
                  "next_check":"下次看修订"}}
    return ReliabilityStore(store).once("losna_research", identity, data, owner=owner)


def labels(store, run):
    for strategy, sym, pct in [("losna_blind",A,3.),("formal_local_fusion_top15",B,1.),
                              ("formal_local_fusion_top15",C,99.)]:
        evidence = {"research_id":"p_"+run} if strategy=="losna_blind" else {}
        conn = store.connect()
        ident = f"{run}_{strategy}_{sym}"
        conn.execute("INSERT INTO selection_candidate_nominations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ident,run,"s",sym,"shadow",strategy,"v1",1,None,0,"eligible",json.dumps(evidence),DAY))
        conn.execute("""INSERT INTO selection_candidate_outcomes
            (nomination_id,horizon_days,entry_date,exit_date,return_pct,outcome_status,evaluated_at,metric_version)
            VALUES (?,5,'2026-10-13','2026-10-19',?,'matured','2026-10-19','signal-price-v2')""",(ident,pct))
        conn.commit()
        conn.close()


def report(store, now=LATER):
    return weekly_contribution(store, week_of=DAY, now=now)


def test_matching_sample_and_denominators_and_private_isolation(store, monkeypatch):
    for name in ("r1","r2","r3","r4","r5","r6"):
        publish(store,name)
    packet(store,"r1")
    packet(store,"r1",identity="private",owner="primary",purpose="portfolio_review")
    packet(store,"r1",identity="directed",purpose="directed_review")
    packet(store,"r2",reviews=False)
    packet(store,"r3",status="claimed")
    packet(store,"r4")
    packet(store,"r4",identity="revised")
    ReliabilityStore(store).once("losna_preparation","r5",{"discovery_status":"data_unavailable"})
    labels(store,"r1")
    result = report(store)
    assert result["counts"] == {"paired":1,"abstained":1,"expired":1,"multiple_versions":1,"missing_input":1,"no_packet":1}
    batch = result["batches"][0]
    assert batch["control_symbols"] == [B] and batch["difference_pp"] == 2
    assert batch["reviews"][0]["in_formal"] is False
    assert batch["reviews"][0]["evidence"] == [{"id":"claim","fact":{"symbol":A,"pe_ttm":6}}]
    assert "private" not in json.dumps(result) and "directed" not in json.dumps(result)
    monkeypatch.setattr("data.research_store.ResearchStore", lambda **kw:store)
    app=FastAPI()
    register_decision_loop_routes(app, agent_result=None,agent_error=None,browser_ok=lambda data:{"ok":True,"data":data})
    client=TestClient(app)
    assert client.get("/api/research/weekly-contribution",params={"week_of":DAY}).json()["data"]["counts"] == result["counts"]
    assert client.get("/api/research/weekly-contribution?week_of=oops").status_code==422
    assert client.get("/api/research/weekly-contribution?week_of=2099-01-01").status_code==422


def test_price_labels_do_not_impute_missing_or_mix_dates_or_packets(store):
    publish(store,"r")
    packet(store,"r")
    labels(store,"r")
    assert report(store,START)["batches"][0]["difference_pp"] is None
    conn=store.connect()
    conn.execute("update selection_candidate_outcomes set exit_date=? where nomination_id like ?", ("2026-10-20", "%losna%"))
    conn.commit()
    assert report(store)["batches"][0]["state"]=="different_basis"
    conn.execute("update selection_candidate_nominations set evidence='{}' where strategy_id='losna_blind'")
    for day in (13,14,15,16,19):
        conn.execute("insert into research_trade_calendar values (?,'test',?)", (f"2026-10-{day}",DAY))
    conn.commit()
    conn.close()
    row=report(store)["batches"][0]
    assert row["state"]=="awaiting_labels" and row["blind"]["matured"]==0 and row["difference_pp"] is None


def test_pair_freezes_before_open_and_marks_using_original_session_only(store):
    capsule=publish(store,"r")
    packet(store,"r")
    batch=report(store,START)["batches"][0]
    assert freeze_pair(store,batch,capsule,clock=lambda:CLOSE) is None
    target=freeze_pair(store,batch,capsule,clock=lambda:START)
    assert target==freeze_pair(store,batch,capsule,clock=lambda:START)
    assert evaluate_pair(target,{},START)["status"]=="pending_close"
    facts = {s:{"symbol":s,"revision":1,"fact_hash":"hash_"+s,"open":10, "close":closing,"trade_date":"2026-10-13",
             "observed_at":"2026-10-13T15:01:00+08:00","provider":"fixture","adjustment":"raw",
             "corporate_actions_complete":True,"volume":10000000,"limit_up":11,"limit_down":9,
             "execution_rules":asdict(ExecutionRules())} for s,closing in [(A,10.8),(B,10.2)]}
    assert evaluate_pair(target,{A:facts[A]},CLOSE)["missing_symbols"]==[B]
    unavailable=deepcopy(facts)
    unavailable[A]["corporate_actions_complete"]=False
    assert evaluate_pair(target,unavailable,CLOSE)["status"]=="missing_facts"
    repriced=deepcopy(facts)
    repriced[A]["execution_rules"]["commission_rate"]="0.01"
    assert evaluate_pair(target,repriced,CLOSE)["status"]=="different_cost_rules"
    locked=deepcopy(facts)
    locked[A]["limit_up"]=10
    assert evaluate_pair(target,locked,CLOSE)["status"]=="no_fills"
    future=deepcopy(facts)
    future[B]["observed_at"]="2026-10-13T17:00:00+08:00"
    assert evaluate_pair(target,future,CLOSE)["missing_symbols"]==[B]
    for symbol,fact in facts.items():
        ReliabilityStore(store).once("execution_fact","2026-10-13:"+symbol,fact)
    assert report(store)["batches"][0]["paper_pair"]["status"]=="missing_facts"
    conn=store.connect()
    conn.execute("UPDATE research_reliability_records SET created_at=? WHERE kind='execution_fact'",
                 ("2026-10-13T07:02:00+00:00",))
    conn.commit()
    conn.close()
    pair=report(store)["batches"][0]["paper_pair"]
    assert pair["status"]=="marked" and float(pair["blind"]["fees"])>0 and 0<pair["difference_pp"]<6
    assert {f["id"] for f in pair["fact_refs"]}=={"2026-10-13:"+s for s in (A,B)}
    assert pair["blind"]["return_pct"]<8 and pair["formal"]["return_pct"]<2
    assert report(store)["batches"][0]["paper_pair"]==pair
    assert ReliabilityStore(store).list("losna_weekly_paper_pair",owner="primary")==[]


def test_existing_followup_job_locks_public_pair_even_when_private_followup_fails(store, monkeypatch):
    from application.decision_loop import DecisionLoopService
    from jobs import jobs_hub
    publish(store,"r")
    packet(store,"r")
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return START.astimezone(tz)
    def fail():
        raise ValueError("private_holdings_unavailable")
    logged=[]
    monkeypatch.setattr("application.weekly_paper_pair.datetime",Frozen)
    monkeypatch.setattr("application.decision_loop.DecisionLoopService",lambda:DecisionLoopService(store))
    monkeypatch.setattr("application.losna_research.prepare_followups",fail)
    monkeypatch.setattr(jobs_hub,"_log_run",lambda name,status,**kwargs:logged.append((name,status)))
    jobs_hub.task_losna_followups()
    assert logged==[("losna_followups","error")]
    saved=ReliabilityStore(store).get("losna_weekly_paper_pair","r")
    assert saved["entry_at"]=="2026-10-13T09:30:00+08:00" and saved["formal"]==[B]
    assert DecisionLoopService(store).dashboard()["model_books"]==[]
