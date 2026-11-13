"""Purpose-scoped Losna research packets backed by the existing revision journal."""
from datetime import datetime, timedelta, timezone
from application.results import payload_hash
from core.research_errors import ResearchRejection
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
        packet = dict(schema_version="losna-research-v2", producer="losna", purpose=purpose, run_id=run_id, input_version=input_version,
                      candidates=candidates, evidence=evidence,
                      created_at=now.isoformat(), deadline=(now + timedelta(minutes=ttl_minutes)).isoformat())
        identity = payload_hash({"purpose": purpose, "input": input_version, "owner": owner})
        return self.repo.once(KIND, identity, {"packet": packet, "status": "pending"}, owner=owner)

    def get(self, identity, owner):
        result = self.repo.get(KIND, identity, owner)
        if not result:
            raise ResearchRejection("research_not_found")
        return result

    def _original_claim(self, cur, owner, worker, claim_id):
        # Include historical generations, even if the packet left the current queue
        # or another worker took its lease. The first grant owns a reused legacy key.
        payload = "CAST(payload AS jsonb)" if self.repo.store._is_postgres else "payload"
        cur.execute(f"SELECT object_id,revision FROM research_reliability_records WHERE kind=? AND owner_id=? "
                    f"AND {payload}->>'worker'=? AND {payload}->>'claim_id'=? "
                    f"AND {payload}->>'status'='claimed' ORDER BY created_at, object_id, revision LIMIT 1",
                    (KIND, owner, worker, claim_id))
        return cur.fetchone()

    def _expire(self, cur, owner, record, now):
        if (record["status"] in {"pending", "claimed"}
                and datetime.fromisoformat(record["packet"]["deadline"]) <= now):
            return self.repo.append(cur, KIND, record["object_id"], owner,
                                    {**record, "status": "expired", "expired_at": now.isoformat()}, record)
        return record

    def claim(self, *, owner, worker, purposes, claim_id=None):
        if not worker or not set(purposes) <= PURPOSES:
            raise ResearchRejection("research_claim_invalid")
        with self.repo.transaction() as cur:
            now = self.clock()
            if claim_id is not None:
                original = self._original_claim(cur, owner, worker, claim_id)
                if original:
                    identity, generation = original
                    bound = self.repo.read(cur, KIND, identity, owner)
                    if bound["packet"]["purpose"] not in purposes:
                        raise ResearchRejection("purpose_denied")
                    expected_revision = generation + (1 if bound["status"] in {"received", "expired"} else 0)
                    if bound["revision"] != expected_revision:
                        raise ResearchRejection("research_generation_conflict", identity=identity)
                    return self._expire(cur, owner, bound, now)
            for item in reversed(self.repo.list_latest(cur, KIND, owner)):
                if item["packet"]["purpose"] not in purposes:
                    continue
                old = self._expire(cur, owner, item, now)
                if old["status"] == "claimed":
                    if old.get("worker") == worker and old.get("claim_id") == (claim_id or worker):
                        return old
                    lease = old.get("lease_until") or old["packet"]["deadline"]
                    if datetime.fromisoformat(lease) > now:
                        continue
                elif old["status"] != "pending":
                    continue
                return self.repo.append(cur, KIND, old["object_id"], owner,
                                        {**old, "status": "claimed", "worker": worker,
                                         "claim_id": claim_id or worker,
                                         "lease_until": min(now + timedelta(minutes=3),
                                             datetime.fromisoformat(old["packet"]["deadline"])).isoformat()}, old)
        return {"status": "empty"}

    def submit(self, *, identity, owner, worker, generation, result):
        if (not isinstance(result, dict) or set(result) != {"reviews", "next_check"}
                or not isinstance(result["next_check"], str) or len(result["next_check"]) > 400):
            raise ResearchRejection("research_result_invalid")
        if not isinstance(result["reviews"], list) or len(result["reviews"]) > 3:
            raise ResearchRejection("research_budget_invalid")
        digest = payload_hash(result)
        with self.repo.transaction() as cur:
            old = self.repo.read(cur, KIND, identity, owner)
            if not old or old.get("worker") != worker:
                raise ResearchRejection("research_not_found")
            if old["status"] in {"received", "expired"}:
                # The terminal revision immediately follows the last claim.
                if generation != old["revision"] - 1:
                    raise ResearchRejection("research_generation_conflict")
                if old["status"] == "expired":
                    raise ResearchRejection("research_deadline_expired")
                if old["result_hash"] != digest:
                    raise ResearchRejection("research_result_conflict")
                return old
            if old["status"] != "claimed" or old["revision"] != generation:
                raise ResearchRejection("research_generation_conflict")
            if datetime.fromisoformat(old["packet"]["deadline"]) <= self.clock():
                raise ResearchRejection("research_deadline_expired")
            symbols = {str(r["symbol"]) for r in old["packet"]["candidates"]}
            refs = {r["evidence_id"] for r in old["packet"]["evidence"]}
            seen = set()
            for review in result["reviews"]:
                if not isinstance(review, dict) or set(review) != {"symbol", "reason", "counterevidence", "evidence_refs"}:
                    raise ResearchRejection("research_fields_invalid")
                symbol = review["symbol"]
                if not isinstance(symbol, str) or symbol not in symbols or symbol in seen:
                    raise ResearchRejection("research_subject_invalid")
                seen.add(symbol)
                if (not isinstance(review["evidence_refs"], list) or not review["evidence_refs"]
                        or any(not isinstance(ref, str) for ref in review["evidence_refs"])
                        or not set(review["evidence_refs"]) <= refs):
                    raise ResearchRejection("research_evidence_invalid")
                if any(not isinstance(review[k], str) or not 1 <= len(review[k]) <= 600
                       for k in ("reason", "counterevidence")):
                    raise ResearchRejection("research_text_invalid")
            return self.repo.append(cur, KIND, identity, owner,
                                    {**old, "status": "received", "result": result,
                                     "result_hash": digest, "received_at": self.clock().isoformat(),
                                     "authoritative": False, "auto_apply": False}, old)


def frozen_features(service, capsule):
    """Compute once from immutable manifest inputs; cache public rows, not ranks."""
    import pandas as pd
    from analysis.independent_selector import IndependentPolicy, _prepare_frame
    from application.results import clean_json
    research = LosnaResearch(service.store)
    manifest = capsule.get("manifest_id")
    if not manifest:
        return pd.DataFrame(), {}
    identity = payload_hash({"manifest": manifest, "schema": "left-features-v2"})
    cached = research.repo.get("losna_feature_manifest", identity)
    if not cached:
        frame, provenance = _prepare_frame(service.store, manifest, IndependentPolicy())
        if frame.empty:
            return frame, provenance
        fields = list(dict.fromkeys(["symbol", "name", "industry", "close", "above_ma60",
            "range_pos_60", "volume_5_vs_60", "amount_5_vs_60", "state", "history_days",
            *IndependentPolicy().required_fields]))
        rows = clean_json(frame[[c for c in fields if c in frame]].to_dict("records"))
        source = {"manifest_id": manifest, "market_as_of": provenance.get("market_as_of"),
                  "market_input_hash": provenance.get("market_input_hash"),
                  "market_input_mode": provenance.get("market_input_mode"),
                  "financial_revision_set_id": (provenance.get("manifest") or {}).get("financial_revision_set_id"),
                  "price_basis": "qfq_frozen_completed_bar_not_execution_quote"}
        source_hash = payload_hash({"source": source, "rows": rows})
        shards = {}
        for row in rows:
            shards.setdefault(str(row["symbol"])[:4], []).append(row)
        identities = []
        for prefix, values in sorted(shards.items()):
            key = payload_hash({"manifest": manifest, "source": source_hash, "prefix": prefix})
            research.repo.once("losna_feature_rows", key, {"rows": values})
            identities.append(key)
        cached = research.repo.once("losna_feature_manifest", identity,
                                     {"shards": identities, "source": source, "row_count": len(rows)})
    rows = []
    for key in cached["shards"]:
        shard = research.repo.get("losna_feature_rows", key)
        if not shard:
            raise ValueError("frozen_feature_shard_missing")
        rows.extend(shard["rows"])
    return pd.DataFrame(rows), cached["source"]


def attach_feature_evidence(service, capsule, candidates, evidence):
    try:
        frame, source = frozen_features(service, capsule)
    except Exception as exc:
        import pandas as pd
        frame, source = pd.DataFrame(), {"manifest_id": capsule.get("manifest_id"),
                                         "status": "unavailable", "error_code": type(exc).__name__}
    by_symbol = {str(r["symbol"]): r for r in frame.to_dict("records")} if not frame.empty else {}
    from application.results import clean_json
    facts = list(evidence)
    enriched = []
    for candidate in candidates:
        symbol = str(candidate["symbol"])
        values = by_symbol.get(symbol)
        fact = {"symbol": symbol, "source": source,
                "frozen_features": clean_json(values) if values is not None else None,
                "coverage": "available" if values is not None else "not_in_eligible_frozen_stock_pool",
                "boundary": "frozen_public_features; missing values remain unknown"}
        ref = payload_hash(fact)
        facts.append({"evidence_id": ref, "fact": fact})
        enriched.append({**candidate, "feature_evidence_ref": ref})
    return enriched, facts


def private_action_evidence(holdings):
    from jobs.jobs_hub import get_indicator_snapshot
    snapshot = get_indicator_snapshot("intraday_decision") or {}
    wanted = {str(r.get("symbol") or r.get("code")) for r in holdings}
    fields = ("symbol", "action", "requested_action", "reason", "requested_reason", "price",
              "quote_as_of", "freshness", "stop_loss", "target_price", "holding_pnl_pct", "action_guard")
    actions = [{k: row[k] for k in fields if k in row}
               for row in snapshot.get("holdings") or [] if str(row.get("symbol")) in wanted]
    fact = {"generated_at": snapshot.get("generated_at"), "final_actions": actions,
            "portfolio_guard": snapshot.get("portfolio_action_guard"),
            "boundary": "historical final decisions; no new order; verify fresh quote and sellable shares"}
    return {"evidence_id": payload_hash(fact), "fact": fact}


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
    reference = (openapi_trial_reference(latest_openapi_shadow(formal.get("artifacts") or {}))
                 if formal.get("run_id") == capsule.get("run_id") else {})
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
    candidates, evidence = attach_feature_evidence(service, capsule, candidates, evidence)
    version = payload_hash({"capsule": capsule, "reference": reference, "evidence": evidence})
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
                and datetime.fromisoformat(packet["deadline"]) > now):
            return {"id": record["object_id"], "reviews": record["result"]["reviews"],
                    "next_check": record["result"]["next_check"]}
    return {}


def strategy_validation_facts(dashboard, store):
    """Bound model input without changing any sample or return metric."""
    def bounded(value, depth=0):
        if depth >= 6:
            return {"omitted_nested_detail": True} if isinstance(value, (dict, list)) else value
        if isinstance(value, str):
            return value if len(value) <= 400 else value[:400] + " [truncated]"
        if isinstance(value, list):
            rows = [bounded(v, depth+1) for v in value[:12]]
            return rows if len(value) <= 12 else {"shown": rows, "total_count": len(value), "truncated": True}
        if isinstance(value, dict):
            priority = {"state", "status", "promotion_ready", "effective_samples", "sample_size", "independent_dates", "blockers", "reason"}
            keys = sorted(value, key=lambda k: (k not in priority, k))
            result = {k: bounded(value[k], depth+1) for k in keys[:40]}
            if len(keys) > 40:
                result["omitted_field_count"] = len(keys)-40
            return result
        return value
    experiments = [{k: trial[k] for k in ("trial_id", "hypothesis_id", "state", "status", "protocol", "evaluation") if k in trial}
                   for trial in (dashboard.get("experiments") or [])[:20]]
    public = {"policy_timeline": bounded(dashboard.get("policy_timeline")),
              "experiments": bounded(experiments), "model_books": bounded(dashboard.get("model_books")),
              "projection": "bounded aggregate evidence; raw factor arrays omitted; full source remains in Foliant",
              "signal_outcomes": {}}
    fields = ("strategy_id", "strategy_version", "metric_version", "lane", "sample_size", "independent_dates",
              "effective_samples", "promotion_blocker", "avg_return_pct", "win_rate_pct", "benchmark_excess_pct",
              "benchmark_sample_size", "worst_drawdown_pct", "worst_mae_pct", "nonoverlapping_price_intervals")
    for horizon in (1, 3, 5, 10, 20):
        try:
            source = store.selection_strategy_evidence(horizon_days=horizon)
            rows = source.get("strategies") or []
            ordered = sorted(rows, key=lambda r: (not any(x in str(r.get("strategy_id", "")).lower() for x in ("wencai", "openapi", "losna")),
                                                 -int(r.get("sample_size") or 0), str(r.get("strategy_id")), str(r.get("strategy_version"))))
            limit = 40 if horizon == 5 else 12
            public["signal_outcomes"][str(horizon)] = {
                "evidence_snapshot_id": source.get("evidence_snapshot_id"), "horizon_days": horizon,
                "source_group_count": len(rows), "shown_group_count": min(limit, len(rows)), "truncated": len(rows)>limit,
                "strategies": [{k: row[k] for k in fields if k in row} for row in ordered[:limit]],
                "portfolio_comparison": {k: v for k, v in (source.get("portfolio_comparison") or {}).items() if k != "runs"}}
        except Exception as exc:
            public["signal_outcomes"][str(horizon)] = {"status": "unavailable", "error_code": type(exc).__name__}
    public["metric_boundary"] = "price labels are observations, not executable net returns; effective_samples and promotion blockers remain authoritative"
    return public


def portfolio_research_preview(*, capsule, context, quotes, now):
    from application.account_preview import build_account_preview
    from jobs.intraday_decision_monitor import assess_quotes
    mode = "post_close" if (now.hour, now.minute) >= (15, 0) else "intraday"
    symbols = {str(r.get("symbol") or r.get("code")) for r in context["holdings"]}
    quality = assess_quotes([{"symbol": symbol} for symbol in symbols], quotes, now, mode=mode)
    preview = build_account_preview(owner_id="primary", capsule=capsule, context=context,
        raw_quotes=quotes, allow_add=False, now=now, quote_ttl_seconds=480,
        quote_freshness_by_symbol={symbol: row.get("freshness") for symbol, row in quality["items"].items()},
        pricing_mode=mode)
    # Packet admission time is recorded by the journal. Keep the input identity
    # bound to business facts rather than the wall clock of a repeated read.
    return {k: v for k, v in preview.items() if k not in {"created_at", "expires_at", "plan_id"}}


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
    public = strategy_validation_facts(dashboard, service.store)
    result["strategy"] = research.create(
        purpose="strategy_validation", input_version=payload_hash({"facts": public, "run_id": capsule["run_id"], "schema": "v2"}),
        candidates=capsule["opportunity_set"]["top15"],
        evidence=[{"evidence_id": payload_hash(public), "fact": public},
                  {"evidence_id": payload_hash({"scope": "strategy_validation"}),
                   "fact": {"scope": "strategy_validation", "task": "核验实验样本、回撤、成本、未成熟与晋级条件；不要求逐票提名；空reviews可通过next_check记录策略结论"}}],
        owner="research", ttl_minutes=120, run_id=capsule["run_id"])
    context = portfolio_db.action_preview_context()
    quotes = datahub.quotes(account_quote_symbols(capsule, context["holdings"]))
    from zoneinfo import ZoneInfo
    preview = portfolio_research_preview(capsule=capsule, context=context, quotes=quotes,
                                         now=datetime.now(ZoneInfo("Asia/Shanghai")))
    if portfolio_db.action_preview_context()["watermark"] != context["watermark"]:
        raise ValueError("holdings_changed_during_review")
    candidates = [{**r, "symbol": str(r.get("symbol") or r.get("code"))}
                  for r in context["holdings"]]
    candidates, evidence = attach_feature_evidence(service, capsule, candidates,
        [{"evidence_id": payload_hash(preview), "fact": preview}, private_action_evidence(context["holdings"])])
    result["portfolio"] = research.create(
        purpose="portfolio_review", input_version=payload_hash({"context": context, "run_id": capsule["run_id"], "evidence": evidence}),
        candidates=candidates,
        evidence=evidence,
        owner="primary", ttl_minutes=120, run_id=capsule["run_id"])
    return result

def prepare_discovery(service, capsule):
    research = LosnaResearch(service.store)
    from analysis.independent_selector import IndependentPolicy, _prepare_frame, _score
    from application.results import clean_json
    manifest_id = capsule.get("manifest_id")
    if manifest_id:
        frame, provenance = frozen_features(service, capsule)
        if not frame.empty:
            eligible, _ = _score(frame, IndependentPolicy())
            complete_count = len(eligible)
            eligible = eligible[(eligible["pe_ttm"] > 0) & (eligible["cash_quality"] > 0)
                                & (eligible["ret_60"] < 0)].sort_values(["pe_ttm", "symbol"])
            fields = ["symbol", "industry", "close", "above_ma60", "range_pos_60", "volume_5_vs_60",
                      "amount_5_vs_60", "state", *IndependentPolicy().required_fields]
            fields = list(dict.fromkeys(c for c in fields if c in eligible.columns))
            sample = clean_json(eligible.head(100)[fields].to_dict("records"))
            if sample:
                facts = [{"evidence_id": payload_hash(row), "fact": row} for row in sample]
                scope = {"eligible_count": len(eligible), "sample_count": len(sample),
                         "complete_count": complete_count,
                         "prefilter": "left-observation-positive-pe-cash-negative-return-v2", "manifest_id": manifest_id,
                         "source": provenance}
                facts.append({"evidence_id": payload_hash(scope), "fact": scope})
                return research.create(purpose="independent_discovery",
                                input_version=payload_hash({"manifest": manifest_id, "sample": sample, "schema": "v2"}),
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
                 "strategy_version": ("losna-blind-v2" if packet.get("schema_version") == "losna-research-v2" else "losna-blind-v1"), "lane": "shadow",
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
