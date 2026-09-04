"""Research decision-loop application service, using the existing warehouse/artifacts.

No broker API, credentials or private holdings are stored in these research tables.
DDL is migration-owned; test adapters can explicitly initialize the extension.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
from application.results import payload_hash
from analysis.decision_evaluation import ExecutionRules, simulate_fill
from analysis.factor_ast import fingerprint
from analysis.research_governance import HYPOTHESES, evidence_summary


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


class DecisionLoopService:
    def __init__(self, store=None):
        if store is None:
            from data.research_store import ResearchStore
            store = ResearchStore(ensure_schema=False)
        self.store = store

    def capsule(self, run_id=None):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            if run_id:
                cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='decision_capsule'",
                            (run_id,))
            else:
                cur.execute("SELECT a.payload FROM selection_artifacts a JOIN selection_runs r ON r.run_id=a.run_id "
                            "WHERE a.artifact_type='decision_capsule' AND r.publication_status='published' "
                            "ORDER BY r.published_at DESC LIMIT 1")
            row = cur.fetchone()
            return _decode(row[0]) if row else None
        finally:
            conn.close()

    def start_model_cohorts(self, capsule):
        from application.research_cases import ResearchCases
        ResearchCases(self.store).seed(capsule)
        """Persist forward intent at publication; future replay cannot invent intent."""
        top15 = capsule["opportunity_set"]["top15"]
        recorded_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        deadline = capsule.get("earliest_execution_at")
        forward = (capsule["recording_mode"] == "contemporaneous"
                   and datetime.fromisoformat(recorded_at) >= datetime.fromisoformat(capsule["published_at"]) and
                   (datetime.fromisoformat(recorded_at) < datetime.fromisoformat(deadline)
                    if deadline else recorded_at[:10] == capsule["published_at"][:10]))
        groups = {"fusion": top15, "top5": capsule["opportunity_set"]["top5"],
                  "without_satellite": [r for r in top15 if r.get("assigned_lane") != "satellite"],
                  "without_timing": [r for r in top15 if r.get("assigned_lane") != "timing"]}
        # PIT-only benchmark comes from persisted nominations, not the core subset
        # that survived fusion. Avoid survivorship bias in the comparison.
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='pit_only_top15'",
                        (capsule["run_id"],))
            pit = cur.fetchone()
            groups["pit_only"] = _decode(pit[0]) if pit else []
            cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='lane_ablation_reruns'",
                        (capsule["run_id"],))
            reruns = cur.fetchone()
            if reruns:
                for name, value in _decode(reruns[0]).items():
                    groups["rerun:" + name] = value["result"]["top15"]
            cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='policy_arm_candidates'",
                        (capsule["run_id"],))
            arms = cur.fetchone()
            if arms:
                for name, value in _decode(arms[0]).items():
                    groups[name] = value["top15"]
            cur.execute("SELECT strategy_id,symbol,lane_rank FROM selection_candidate_nominations "
                        "WHERE selection_run_id=? AND eligibility='eligible' ORDER BY strategy_id,lane_rank",
                        (capsule["run_id"],))
            for strategy_id, symbol, _rank in cur.fetchall():
                if strategy_id == "technical_timing_genome" or str(strategy_id).startswith("local_"):
                    members = groups.setdefault("strategy:" + strategy_id, [])
                    if len(members) < 5 and not any(r["symbol"] == symbol for r in members):
                        members.append({"symbol": symbol})
            created = 0
            for baseline, candidates in groups.items():
                for candidate in candidates:
                    symbol = str(candidate["symbol"])
                    identity = payload_hash({"capsule": capsule["capsule_id"], "baseline": baseline, "symbol": symbol})
                    value = {"order_id": identity, "capsule_id": capsule["capsule_id"], "symbol": symbol,
                             "side": "buy", "published_at": capsule["published_at"],
                             "earliest_execution_at": capsule["earliest_execution_at"],
                             "recording_mode": "contemporaneous" if forward else "backfilled",
                             "recorded_at": recorded_at,
                             "cash_budget": str(round(100000 / max(1, len(candidates)), 2)),
                             "status": "pending", "baseline": baseline,
                             "cohort_type": "independent_equal_cash_cohort_not_continuous_nav"}
                    cur.execute("INSERT INTO research_model_orders "
                                "(order_id,run_id,baseline,symbol,state,payload,created_at,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO NOTHING",
                                (identity, capsule["run_id"], baseline, symbol, "pending", _encode(value),
                                 recorded_at, recorded_at))
                    created += max(0, cur.rowcount)
            conn.commit()
            from application.model_portfolios import ModelPortfolios
            ModelPortfolios(self.store).publish(capsule, groups)
            return {"created": created, "baseline_sizes": {k: len(v) for k, v in groups.items()}}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def settle_models(self, execution_facts, *, now, limit=500):
        """Consume explicit dated facts. Missing facts remain unfilled/pending, not zero returns."""
        if not execution_facts:
            return {}
        conn = self.store.connect()
        counts = {}
        try:
            cur = conn.cursor()
            # Do not let an old missing-data backlog monopolize the first 500
            # rows forever. Admit only securities/dates available in this pass.
            symbols = sorted({key[0] for key in execution_facts})
            days = sorted({key[1] for key in execution_facts})
            date_predicates = " OR ".join("payload LIKE ?" for _ in days)
            cur.execute("SELECT order_id,payload FROM research_model_orders WHERE state='pending' "
                        f"AND symbol IN ({','.join('?' for _ in symbols)}) "
                        f"AND ({date_predicates} OR payload LIKE ?) ORDER BY created_at LIMIT ?",
                        (*symbols, *(f'%"earliest_execution_at":"{day}T%' for day in days),
                         '%"earliest_execution_at":null%', max(1, min(2000, limit))))
            for order_id, raw in cur.fetchall():
                order = _decode(raw)
                earliest = order.get("earliest_execution_at")
                if not earliest:
                    # Resolve a calendar gap honestly without modifying frozen capsule.
                    cur.execute("SELECT MIN(trade_date) FROM research_trade_calendar WHERE trade_date>?",
                                (order["published_at"][:10],))
                    row = cur.fetchone()
                    earliest = f"{row[0]}T09:30:00+08:00" if row and row[0] else None
                    order["earliest_execution_at"] = earliest
                if not earliest or datetime.fromisoformat(now) < datetime.fromisoformat(earliest):
                    continue
                fact = execution_facts.get((order["symbol"], earliest[:10]))
                if order["recording_mode"] != "contemporaneous":
                    result = {"status": "unfilled", "reason": "backfilled_not_forward"}
                elif fact is None:
                    continue  # Missing original facts are recoverable, not an expired trading signal.
                else:
                    from decimal import Decimal
                    try:
                        rules = ExecutionRules(**fact["execution_rules"])
                        price = Decimal(str(fact["open"]))
                        quantity = int(Decimal(order["cash_budget"]) / price) // rules.buy_step * rules.buy_step
                        order["quantity"] = quantity
                        result = simulate_fill(order, fact, rules, cash=order["cash_budget"])
                    except (KeyError, ArithmeticError, ValueError):
                        result = {"status": "unfilled", "reason": "invalid_execution_facts"}
                order.update(result=result, status=result["status"],
                             fact_hash=payload_hash(fact) if fact else None)
                # Compare state prevents duplicate settlement by concurrent workers.
                cur.execute("UPDATE research_model_orders SET state=?,payload=?,updated_at=? "
                            "WHERE order_id=? AND state='pending'",
                            (result["status"], _encode(order), now, order_id))
                if cur.rowcount:
                    counts[result["status"]] = counts.get(result["status"], 0) + 1
            conn.commit()
            return counts
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_trial(self, *, hypothesis_id, ast, dataset_id, data_class, code_revision,
                       parent_trial=None, adapter="factor_ast", now=None):
        from analysis.validation_protocol import DEFAULT_PROTOCOL
        from dataclasses import replace
        if hypothesis_id not in HYPOTHESES or data_class not in {
            "strict_observed_pit", "reconstructed_history", "exploratory"}:
            raise ValueError("invalid_research_registration")
        if adapter not in {"factor_ast", "qlib_offline", "rd_agent_offline", "learning_rank_offline"}:
            raise ValueError("unsupported_offline_adapter")
        if not dataset_id or not code_revision:
            raise ValueError("research_provenance_required")
        hypothesis = HYPOTHESES[hypothesis_id]
        try:
            formula = fingerprint(ast, hypothesis["fields"])
        except (ValueError, TypeError) as exc:
            from data.reliability_store import ReliabilityStore
            import uuid
            ReliabilityStore(self.store).once("research_attempt", uuid.uuid4().hex,
                {"hypothesis_id": hypothesis_id, "state": "compile_failed", "error_category": type(exc).__name__})
            raise
        fingerprint_value = payload_hash({"formula": formula, "dataset": dataset_id, "adapter": adapter})
        now = now or datetime.now().astimezone().isoformat()
        trial_id = "trial_" + payload_hash({"fingerprint": fingerprint_value, "created_at": now})
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482716)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT COUNT(*) FROM research_experiments WHERE hypothesis_id=?", (hypothesis_id,))
            attempted = cur.fetchone()[0]
            cur.execute("SELECT payload FROM research_reliability_records WHERE kind='research_attempt' AND owner_id='research'")
            attempted += sum(_decode(r[0]).get("hypothesis_id") == hypothesis_id for r in cur.fetchall())
            cur.execute("SELECT trial_id FROM research_experiments WHERE fingerprint=? LIMIT 1", (fingerprint_value,))
            duplicate = cur.fetchone()
            state = "duplicate" if duplicate else "budget_exhausted" if attempted >= hypothesis["trial_budget"] else "registered"
            value = {"trial_id": trial_id, "hypothesis_id": hypothesis_id, "ast": ast,
                     "formula_hash": formula, "dataset_id": dataset_id, "data_class": data_class,
                     "code_revision": code_revision, "parent_trial": parent_trial, "adapter": adapter,
                     "baseline": hypothesis["baseline"], "metric": hypothesis["metric"],
                     "validation_protocol": replace(DEFAULT_PROTOCOL, metric=hypothesis["metric"]).__dict__,
                     "search_counts": {"attempts": attempted + 1, "candidates": 1 if state == "registered" else 0,
                                       "metric_views": 0, "holdout_accesses": 0},
                     "state": state, "duplicate_of": duplicate[0] if duplicate else None,
                     "retire_when": "budget exhausted, invalid data or non-positive independent net evidence",
                     "created_at": now}
            cur.execute("INSERT INTO research_experiments VALUES (?,?,?,?,?,?,?)",
                        (trial_id, hypothesis_id, fingerprint_value, state, _encode(value), now, now))
            conn.commit()
            return value
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish_trial(self, trial_id, *, observations=None, error_category=None, diagnostics=None, now=None):
        now = now or datetime.now().astimezone().isoformat()
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload,state FROM research_experiments WHERE trial_id=?", (trial_id,))
            row = cur.fetchone()
            if not row or row[1] != "registered":
                raise ValueError("trial_not_registered")
            trial = _decode(row[0])
            cur.execute("SELECT COUNT(*) FROM research_experiments WHERE hypothesis_id=?", (trial["hypothesis_id"],))
            attempts = cur.fetchone()[0]
            attempts = max(attempts, trial.get("search_counts", {}).get("attempts", 1))
            from analysis.validation_protocol import validate_trial_rows
            checked = validate_trial_rows(observations or [], trial) if observations else []
            evidence = evidence_summary(checked, trials_attempted=attempts)
            evidence["development_candidate_ready"] = evidence["promotion_ready"]
            evidence["promotion_ready"] = False  # Development is not the sealed final evaluation.
            state = "failed" if error_category else "evaluated"
            trial.update(state=state, evidence=evidence,
                         diagnostics=diagnostics or {},
                         error_category=str(error_category)[:80] if error_category else None)
            trial.setdefault("search_counts", {})["metric_views"] = 1 if observations or diagnostics else 0
            cur.execute("UPDATE research_experiments SET state=?,payload=?,updated_at=? WHERE trial_id=? AND state='registered'",
                        (state, _encode(trial), now, trial_id))
            if not cur.rowcount:
                raise ValueError("trial_compare_and_swap_conflict")
            conn.commit()
            return trial
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def consume_holdout(self, batch_id, trial_id, *, evaluator=None, now=None):
        """Reserve-before-view and persist actual evaluation before final retirement.

        ``evaluator`` receives only batch dates plus the frozen trial definition;
        callers cannot use this lifecycle method as a metric-free promotion stamp.
        A crash leaves ``evaluating`` and cannot be silently retried/viewed again.
        """
        now = now or datetime.now().astimezone().isoformat()
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT state,payload FROM research_experiments WHERE trial_id=?", (trial_id,))
            row = cur.fetchone()
            if not row or row[0] != "evaluated":
                raise ValueError("holdout_requires_evaluated_trial")
            trial = _decode(row[1])
            cur.execute("SELECT start_date,end_date FROM research_holdout_batches WHERE batch_id=?", (batch_id,))
            batch = cur.fetchone()
            if not batch or str(batch[1]) >= now[:10]:
                raise ValueError("holdout_not_matured")
            if evaluator is None:
                raise ValueError("holdout_evaluator_required")
            cur.execute("UPDATE research_holdout_batches SET state='evaluating',consumed_by=?,consumed_at=? "
                        "WHERE batch_id=? AND state='sealed'", (trial_id, now, batch_id))
            if cur.rowcount != 1:
                raise ValueError("holdout_already_consumed_or_missing")
            trial.setdefault("search_counts", {})["holdout_accesses"] = trial.get("search_counts", {}).get("holdout_accesses", 0) + 1
            cur.execute("UPDATE research_experiments SET payload=?,updated_at=? WHERE trial_id=?",
                        (_encode(trial), now, trial_id))
            conn.commit()
            reserved = {"batch_id": batch_id, "state": "evaluating", "consumed_by": trial_id,
                        "start_date": str(batch[0]), "end_date": str(batch[1])}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        try:
            from analysis.validation_protocol import ValidationProtocol, validate_trial_rows
            protocol = ValidationProtocol(**trial["validation_protocol"])
            evaluated = validate_trial_rows(evaluator(reserved, trial), trial)
            if any(r["label_start"] < reserved["start_date"] or r["label_end"] > reserved["end_date"] for r in evaluated):
                raise ValueError("evaluation_outside_reserved_batch")
            evidence = evidence_summary(evaluated, trials_attempted=trial["search_counts"]["attempts"],
                                        block_days=protocol.block_days)
            if trial["data_class"] != "strict_observed_pit":
                evidence["promotion_ready"] = False
        except Exception:
            # Keep reservation and crash evidence. Operator review can void it;
            # automatic replay would leak another view of the sealed batch.
            raise
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            result = {"batch_id": batch_id, "trial_id": trial_id, "evidence": evidence,
                      "row_count": len(evaluated), "evaluated_at": now, "promotion_ready": evidence["promotion_ready"],
                      "protocol": trial["validation_protocol"], "formula_hash": trial["formula_hash"],
                      "dataset_id": trial["dataset_id"], "code_revision": trial["code_revision"],
                      "evaluated_rows_hash": payload_hash(evaluated)}
            cur.execute("INSERT INTO research_artifacts "
                        "(artifact_id,subject,artifact_kind,run_id,formal,schema_version,payload_hash,payload,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", ("holdout_" + batch_id, trial_id, "holdout-evaluation", batch_id,
                        0, "holdout-evaluation-v2", payload_hash(result), _encode(result), now))
            cur.execute("UPDATE research_holdout_batches SET state='retired' WHERE batch_id=? AND state='evaluating' AND consumed_by=?",
                        (batch_id, trial_id))
            if cur.rowcount != 1:
                raise ValueError("holdout_finalization_conflict")
            conn.commit()
            return {**reserved, "state": "retired", "evaluation": result}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def void_holdout(self, batch_id, trial_id, *, reason):
        """Operator-only terminal audit; never reopen a revealed batch."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("holdout_void_reason_required")
        from data.reliability_store import ReliabilityStore
        with ReliabilityStore(self.store).transaction() as cur:
            old = ReliabilityStore.read(cur, "holdout_void", batch_id, "research")
            if old:
                if old["trial_id"] != trial_id:
                    raise ValueError("holdout_owner_mismatch")
                return old
            cur.execute("UPDATE research_holdout_batches SET state='retired' WHERE batch_id=? AND consumed_by=? AND state='evaluating'",
                        (batch_id, trial_id))
            if cur.rowcount != 1:
                raise ValueError("holdout_not_interrupted_or_owner_mismatch")
            return ReliabilityStore.append(cur, "holdout_void", batch_id, "research", {
                "batch_id": batch_id, "trial_id": trial_id, "status": "void", "reason": reason.strip(),
                "promotion_ready": False, "revealed_interval_reusable": False})

    def seal_holdout(self, *, start_date, end_date, now=None):
        from datetime import date
        now = now or datetime.now().astimezone().isoformat()
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start > end or start_date <= now[:10]:
            raise ValueError("holdout_must_be_preregistered_in_future")
        identity = "holdout_" + payload_hash({"start": start_date, "end": end_date})
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482716)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT batch_id,state FROM research_holdout_batches WHERE start_date<=? AND end_date>=?", (end_date, start_date))
            existing = cur.fetchone()
            if existing:
                if existing[0] == identity and existing[1] == "sealed":
                    conn.rollback()
                    return {"batch_id": identity, "state": "sealed", "idempotent": True}
                raise ValueError("overlapping_sealed_holdout")
            cur.execute("INSERT INTO research_holdout_batches VALUES (?,?,?,?,?,?)",
                        (identity, start_date, end_date, "sealed", None, None))
            conn.commit()
            return {"batch_id": identity, "state": "sealed"}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rollback_policy(self, *, target_hash, current_hash, reason):
        """Operator-only compensating publication; never deletes policy history."""
        if not str(reason).strip():
            raise ValueError("rollback_reason_required")
        identity = payload_hash({"base": current_hash, "target": target_hash, "reason": reason})
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT proposal_id FROM strategy_adjustment_proposals WHERE proposal_id=?",
                        ("rollback_" + identity,))
            if cur.fetchone():
                return {"status": "rolled_back", "proposal_id": "rollback_" + identity,
                        "target": target_hash, "idempotent": True}
            cur.execute("SELECT payload FROM strategy_policy_versions WHERE policy_hash=? LIMIT 1", (target_hash,))
            row = cur.fetchone()
            if not row:
                raise ValueError("rollback_target_not_recorded")
            policy = _decode(row[0])
        finally:
            conn.close()
        now = datetime.now().astimezone().isoformat()
        policy["version"] = f"rollback-{identity[:12]}"
        proposal = {"proposal_id": "rollback_" + identity, "base_policy_hash": current_hash,
                    "evidence_snapshot_id": "operator-rollback:" + target_hash,
                    "effective_from": now, "reason": reason, "rollback_target": target_hash}
        self.store.save_strategy_policy_proposal(proposal, validation_status="rolled_back",
                                                validation_reason=reason, applied_policy=policy)
        return {"status": "rolled_back", "proposal_id": proposal["proposal_id"], "target": target_hash}

    def dashboard(self):
        from application.research_cases import ResearchCases
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT baseline,state,COUNT(*) FROM research_model_orders GROUP BY baseline,state")
            models = [{"baseline": r[0], "state": r[1], "count": r[2]} for r in cur.fetchall()]
            cur.execute("SELECT payload FROM research_experiments ORDER BY created_at DESC LIMIT 30")
            experiments = [_decode(r[0]) for r in cur.fetchall()]
            for trial in experiments:
                for values in (trial.get("diagnostics", {}).get("factor_values") or {}).values():
                    values.pop("inputs", None)
                    values.pop("dates", None)
            cur.execute("SELECT batch_id,state,consumed_by FROM research_holdout_batches ORDER BY end_date DESC LIMIT 12")
            holdouts = [{"batch_id": r[0], "state": r[1], "consumed_by": r[2]} for r in cur.fetchall()]
            cur.execute("SELECT a.payload FROM selection_artifacts a JOIN selection_runs r ON r.run_id=a.run_id "
                        "WHERE a.artifact_type='decision_capsule' AND r.publication_status='published' "
                        "ORDER BY r.published_at DESC LIMIT 2")
            capsules = [_decode(r[0]) for r in cur.fetchall()]
            from application.decision_capsule import context_diff
            diff = context_diff(capsules[1], capsules[0]) if len(capsules) == 2 else {}
            cur.execute("SELECT validation_status,validation_reason,created_at FROM strategy_adjustment_proposals "
                        "ORDER BY created_at DESC LIMIT 12")
            policies = [{"status": r[0], "reason": r[1], "created_at": r[2]} for r in cur.fetchall()]
            cur.execute("SELECT payload FROM research_artifacts WHERE artifact_kind='committee-reservation' "
                        "ORDER BY created_at DESC LIMIT 1")
            reservation = cur.fetchone()
            cur.execute("SELECT baseline,payload FROM research_model_portfolios ORDER BY baseline")
            model_books = []
            for baseline, payload in cur.fetchall():
                ledger = _decode(payload)
                model_books.append({"baseline": baseline, "revision": ledger["revision"],
                                    "latest": ledger["marks"][-1] if ledger["marks"] else None,
                                    "fees_paid": ledger["fees"], "mark_count": len(ledger["marks"])})
            return {"schema_version": "decision-loop-dashboard-v1", "scope": "research",
                    "research_cases": ResearchCases(self.store).view(),
                    "capsule": capsules[0] if capsules else None, "context_diff": diff,
                    "policy_timeline": policies, "research_budget": _decode(reservation[0]) if reservation else None,
                    "model_orders": models, "experiments": experiments,
                    "model_books": model_books,
                    "holdouts": holdouts, "hypotheses": HYPOTHESES,
                    "books": [{"kind": "signal", "metric": "price_return_pct", "description": "价格后验，不是净交易收益"},
                              {"kind": "model", "metric": "net_model_nav", "description": "声明规则的模拟成交；未成交不算零收益"},
                              {"kind": "account", "metric": "reconciled_pnl", "description": "私人账户，需单独授权读取"}]}
        finally:
            conn.close()

    def compare_offline(self, payload):
        """Operator-only comparison; append result to the existing artifact store."""
        from analysis.offline_comparison import compare_exports
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT start_date,end_date FROM research_holdout_batches WHERE state='sealed'")
            result = compare_exports(payload, sealed_intervals=[tuple(r) for r in cur.fetchall()])
            identity = result["input_hash"]
            now = datetime.now().astimezone().isoformat()
            cur.execute("INSERT INTO research_artifacts "
                        "(artifact_id,subject,artifact_kind,run_id,formal,schema_version,payload_hash,payload,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,artifact_kind) DO NOTHING",
                        ("comparison_" + identity, "offline-comparison", "offline-model-comparison", identity,
                         0, result["schema_version"], payload_hash(result), _encode(result), now))
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
