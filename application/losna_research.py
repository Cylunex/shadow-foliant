"""Purpose-scoped Losna research packets backed by the existing revision journal."""
from datetime import datetime, timedelta, timezone
from application.results import payload_hash
from data.reliability_store import ReliabilityStore

PURPOSES = {"independent_discovery", "directed_review", "strategy_validation", "portfolio_review"}
KIND = "losna_research"


class LosnaResearch:
    def __init__(self, store, clock=None):
        self.repo = ReliabilityStore(store)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, *, purpose, input_version, candidates, evidence, owner, ttl_minutes=15, run_id=None):
        if purpose not in PURPOSES or not owner or not input_version:
            raise ValueError("research_scope_invalid")
        if purpose != "portfolio_review" and owner != "research":
            raise ValueError("public_research_owner_required")
        if purpose == "portfolio_review" and owner == "research":
            raise ValueError("private_owner_required")
        if not 1 <= ttl_minutes <= 120 or len(candidates) > 100 or len(evidence) > 200:
            raise ValueError("research_budget_invalid")
        now = self.clock()
        packet = dict(producer="losna", purpose=purpose, run_id=run_id, input_version=input_version,
                      candidates=candidates, evidence=evidence,
                      created_at=now.isoformat(), deadline=(now + timedelta(minutes=ttl_minutes)).isoformat())
        identity = payload_hash({"purpose": purpose, "input": input_version, "owner": owner})
        return self.repo.once(KIND, identity, {"packet": packet, "status": "pending"}, owner=owner)

    def get(self, identity, owner):
        result = self.repo.get(KIND, identity, owner)
        if not result:
            raise ValueError("research_not_found")
        return result

    def claim(self, *, owner, worker, purposes):
        if not worker or not set(purposes) <= PURPOSES:
            raise ValueError("research_claim_invalid")
        for item in reversed(self.repo.list(KIND, owner=owner)):
            if item["packet"]["purpose"] not in purposes:
                continue
            with self.repo.transaction() as cur:
                old = self.repo.read(cur, KIND, item["object_id"], owner)
                if old["status"] == "claimed" and old.get("worker") == worker and datetime.fromisoformat(old["packet"]["deadline"]) > self.clock():
                    return old
                if old["status"] != "pending" or datetime.fromisoformat(old["packet"]["deadline"]) <= self.clock():
                    continue
                return self.repo.append(cur, KIND, item["object_id"], owner,
                                        {**old, "status": "claimed", "worker": worker}, old)
        return {"status": "empty"}

    def submit(self, *, identity, owner, worker, generation, result):
        if set(result) != {"reviews", "next_check"} or not isinstance(result["next_check"], str) or len(result["next_check"]) > 400:
            raise ValueError("research_result_invalid")
        if not isinstance(result["reviews"], list) or len(result["reviews"]) > 3:
            raise ValueError("research_budget_invalid")
        digest = payload_hash(result)
        with self.repo.transaction() as cur:
            old = self.repo.read(cur, KIND, identity, owner)
            if not old or old.get("worker") != worker:
                raise ValueError("research_not_found")
            if old["status"] == "received":
                if old["result_hash"] != digest:
                    raise ValueError("research_result_conflict")
                return old
            if old["status"] != "claimed" or old["revision"] != generation:
                raise ValueError("research_generation_conflict")
            if datetime.fromisoformat(old["packet"]["deadline"]) <= self.clock():
                raise ValueError("research_deadline_expired")
            symbols = {str(r["symbol"]) for r in old["packet"]["candidates"]}
            refs = {r["evidence_id"] for r in old["packet"]["evidence"]}
            seen = set()
            for review in result["reviews"]:
                if set(review) != {"symbol", "reason", "counterevidence", "evidence_refs"}:
                    raise ValueError("research_fields_invalid")
                symbol = review["symbol"]
                if symbol not in symbols or symbol in seen:
                    raise ValueError("research_subject_invalid")
                seen.add(symbol)
                if (not isinstance(review["evidence_refs"], list) or not review["evidence_refs"]
                        or any(not isinstance(ref, str) for ref in review["evidence_refs"])
                        or not set(review["evidence_refs"]) <= refs):
                    raise ValueError("research_evidence_invalid")
                if any(not isinstance(review[k], str) or not 1 <= len(review[k]) <= 600
                       for k in ("reason", "counterevidence")):
                    raise ValueError("research_text_invalid")
            return self.repo.append(cur, KIND, identity, owner,
                                    {**old, "status": "received", "result": result,
                                     "result_hash": digest, "received_at": self.clock().isoformat(),
                                     "authoritative": False, "auto_apply": False}, old)


def prepare_reviews():
    """Queue existing facts; no recomputation, trading, or notification."""
    from application.decision_loop import DecisionLoopService
    service = DecisionLoopService()
    capsule = service.capsule()
    if not capsule:
        return {"status": "missing"}
    research = LosnaResearch(service.store)
    try:
        discovery = prepare_discovery(service, capsule)
        discovery_status = "prepared" if discovery else "data_missing"
    except Exception:
        discovery_status = "data_unavailable"
    research.repo.once("losna_preparation", str(capsule["run_id"]),
                       {"discovery_status": discovery_status}, owner="research")
    candidates = capsule.get("opportunity_set", {}).get("top15") or []
    # Each evidence reference binds the exact frozen row, not a model assertion.
    evidence = [{"evidence_id": payload_hash(row), "fact": row} for row in candidates]
    from selection.reference_display import latest_openapi_shadow, openapi_trial_reference
    formal = service.store.latest_formal_selection() or {}
    reference = openapi_trial_reference(latest_openapi_shadow(formal.get("artifacts") or {}))
    known = {row["symbol"] for row in candidates}
    candidates = list(candidates)
    for group in (reference.get("strategies") or {}).values():
        if group.get("status") != "trial_verified":
            continue
        for pick in group.get("picks") or []:
            if pick["symbol"] not in known:
                candidates.append({**pick, "source": "wencai_observation_only"})
                known.add(pick["symbol"])
        evidence.append({"evidence_id": payload_hash(group), "fact": group})
    version = payload_hash({"capsule": capsule, "reference": reference})
    return research.create(purpose="directed_review", input_version=version,
                           candidates=candidates, evidence=evidence, owner="research", run_id=capsule["run_id"])


def latest_review(run_id, *, now, store=None):
    if store is None:
        from data.research_store import ResearchStore
        store = ResearchStore(ensure_schema=False)
    for record in ReliabilityStore(store).list(KIND, owner="research"):
        packet = record["packet"]
        if (record["status"] == "received" and packet["purpose"] in {"directed_review", "independent_discovery"}
                and packet.get("run_id") == run_id
                and datetime.fromisoformat(record["received_at"]) <= now
                and datetime.fromisoformat(packet["deadline"]).date() == now.date()):
            return {"id": record["object_id"], "reviews": record["result"]["reviews"],
                    "next_check": record["result"]["next_check"]}
    return {}


def prepare_followups():
    from application.decision_loop import DecisionLoopService
    from portfolio_db import portfolio_db
    from application.account_preview import build_account_preview, account_quote_symbols
    import datahub
    service = DecisionLoopService()
    capsule = service.capsule()
    if not capsule:
        return {"status": "missing"}
    research = LosnaResearch(service.store)
    result = {"registered_nominations": register_forward_nominations(service.store)}
    dashboard = service.dashboard()
    public = {key: dashboard.get(key) for key in ("policy_timeline", "experiments", "model_books")}
    result["strategy"] = research.create(
        purpose="strategy_validation", input_version=payload_hash(public),
        candidates=capsule["opportunity_set"]["top15"],
        evidence=[{"evidence_id": payload_hash(public), "fact": public}],
        owner="research", ttl_minutes=120, run_id=capsule["run_id"])
    context = portfolio_db.action_preview_context()
    quotes = datahub.quotes(account_quote_symbols(capsule, context["holdings"]))
    preview = build_account_preview(owner_id="primary", capsule=capsule, context=context,
                                    raw_quotes=quotes, allow_add=False)
    if portfolio_db.action_preview_context()["watermark"] != context["watermark"]:
        raise ValueError("holdings_changed_during_review")
    candidates = [{**r, "symbol": str(r.get("symbol") or r.get("code"))}
                  for r in context["holdings"]]
    result["portfolio"] = research.create(
        purpose="portfolio_review", input_version=payload_hash(context),
        candidates=candidates,
        evidence=[{"evidence_id": payload_hash(preview), "fact": preview}],
        owner="primary", ttl_minutes=120, run_id=capsule["run_id"])
    return result

def prepare_discovery(service, capsule):
    research = LosnaResearch(service.store)
    from analysis.independent_selector import IndependentPolicy, _prepare_frame, _score
    from application.results import clean_json
    manifest_id = capsule.get("manifest_id")
    if manifest_id:
        frame, provenance = _prepare_frame(service.store, manifest_id, IndependentPolicy())
        if not frame.empty:
            eligible, _ = _score(frame, IndependentPolicy())
            complete_count = len(eligible)
            eligible = eligible[(eligible["pe_ttm"] > 0) & (eligible["cash_quality"] > 0)
                                & (eligible["ret_60"] < 0)].sort_values(["pe_ttm", "symbol"])
            fields = ["symbol", "industry", *IndependentPolicy().required_fields]
            fields = list(dict.fromkeys(c for c in fields if c in eligible.columns))
            sample = clean_json(eligible.head(100)[fields].to_dict("records"))
            if sample:
                facts = [{"evidence_id": payload_hash(row), "fact": row} for row in sample]
                scope = {"eligible_count": len(eligible), "sample_count": len(sample),
                         "complete_count": complete_count,
                         "prefilter": "left-observation-positive-pe-cash-negative-return-v1", "manifest_id": manifest_id}
                facts.append({"evidence_id": payload_hash(scope), "fact": scope})
                return research.create(purpose="independent_discovery",
                                input_version=payload_hash({"manifest": manifest_id, "sample": sample}),
                                candidates=sample, evidence=facts, owner="research",
                                run_id=capsule["run_id"])


def register_forward_nominations(store):
    """Project accepted blind proposals into the existing mature-outcome pipeline."""
    count = 0
    for record in ReliabilityStore(store).list(KIND, owner="research"):
        packet = record["packet"]
        if record["status"] != "received" or packet["purpose"] != "independent_discovery":
            continue
        rows = [{"symbol": row["symbol"], "strategy_id": "losna_blind",
                 "strategy_version": "losna-blind-v1", "lane": "shadow",
                 "lane_rank": rank, "priority_weight": 0,
                 "evidence": {"research_id": record["object_id"], "reason": row["reason"],
                              "counterevidence": row["counterevidence"], "refs": row["evidence_refs"],
                              "received_at": record["received_at"], "auto_apply": False}}
                for rank, row in enumerate(record["result"]["reviews"], 1)]
        count += store.save_selection_strategy_records(
            packet["run_id"], rows, policy={"version": "losna-shadow-v1"},
            policy_hash=packet["input_version"], selection_date=packet["created_at"][:10],
            input_snapshot_id=packet["input_version"], persist_policy=False, idempotent=True)
    return count
