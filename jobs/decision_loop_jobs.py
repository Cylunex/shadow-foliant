"""Bounded additions to existing schedules; no extra weekend LLM window."""
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo


def refresh_quality(store=None):
    """Call from synchronization jobs, never from a web health probe."""
    from core.research_health import refresh_quality_report, _default_mode
    try:
        report = refresh_quality_report(store=store)
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if _default_mode(day) == "postclose":
            # Refresh both generation-keyed caches after evening ingestion;
            # HTTP health probes still never score the full market.
            refresh_quality_report(store=store, selection_date=day, mode="preopen")
        return {"status": report["status"], "report_id": report.get("report_id")}
    except Exception as exc:
        print(f"[quality-report] refresh failed: {type(exc).__name__}", flush=True)
        return {"status": "failed", "error_category": type(exc).__name__}


def cached_quality(store=None):
    """Read the quality artifact produced by the research-sync jobs.

    The daily decision loop is part of ``eod_outcomes`` and has a hard 1800s
    budget.  Re-scoring the full market here duplicated the earlier research
    sync and could consume that entire budget.  Quality is optional evidence
    for this loop, so a missing/stale artifact is reported explicitly without
    recomputing it inline.
    """
    try:
        from core.research_health import cached_snapshot

        report = cached_snapshot(store=store)
        return {
            "status": report.get("status") or "stale",
            "report_id": report.get("report_id"),
            "ready": bool(report.get("ready")),
            "source": "cached_research_sync_artifact",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error_category": type(exc).__name__,
            "source": "cached_research_sync_artifact",
        }


def _execution_fact(symbol, day, quote, provider):
    from analysis.decision_evaluation import equity_rules, missing_execution_fields
    from data.valuation_contract import closing_timestamp, number

    timestamp = closing_timestamp(quote.get("quote_time") or quote.get("observed_at"), day)
    rules = equity_rules(symbol)
    prices = {key: number(quote.get(field)) for key, field in (
        ("open", "open"), ("close", "price"), ("limit_up", "limit_up"), ("limit_down", "limit_down"))}
    volume = number(quote.get("volume"))
    fact = {"trade_date": day, "adjustment": "raw", **prices, "volume": volume,
            "previous_close": number(quote.get("last_close")),
            "suspended": volume == 0, "provider": provider,
            "observed_at": timestamp, "execution_rules": asdict(rules) if rules else None}
    missing = missing_execution_fields(fact, day)
    if not timestamp:
        missing.append("same_day_closing_timestamp")
    if not prices["close"] or prices["close"] <= 0:
        missing.append("close")
    return (None, missing or ["no_quote"]) if missing or not quote else (fact, [])


def _historical_acquirer(archive):
    from analysis.decision_evaluation import missing_execution_fields
    from application.results import payload_hash
    from data.sources.zzshare import available, historical_execution_session

    remaining = 3

    def acquire(day, symbols):
        nonlocal remaining
        if not symbols or not remaining or not available():
            return {}
        work_id = archive.repo.enqueue("historical_execution", f"{day}:{payload_hash(sorted(symbols))}",
                                       {"trade_date": day})
        work = archive.repo.claim("historical_execution", work_id=work_id, limit=1)
        if not work:
            return {}
        remaining -= 1
        incoming, detail = historical_execution_session(day, symbols)
        records = {}
        gaps = {}
        for symbol in sorted(symbols):
            fact = incoming.get(symbol)
            if fact:
                coverage = archive.repo.get("corporate_coverage", f"{day}:{symbol}")
                if coverage:
                    fact["corporate_coverage"] = coverage
                    fact["corporate_action_unresolved"] = any(
                        event.get("ex_date") == day and event.get("status") == "requires_entitlement_and_tax"
                        for event in coverage.get("events", []))
                records[(symbol, day)] = fact
            if missing := missing_execution_fields(fact, day):
                gaps[symbol] = missing
        try:
            facts = archive.record_many(records, historical=True)
        except Exception as exc:
            archive.repo.record_work_outcomes([(work[0], {**detail, "archive_error": type(exc).__name__}, False)])
            raise
        archive.repo.record_work_outcomes([(work[0], {**detail, "missing": gaps}, not gaps)])
        return facts

    return acquire


def daily_decision_loop(store=None):
    from application.decision_loop import DecisionLoopService
    from application.model_portfolios import ModelPortfolios
    from application.settlement_evidence import SettlementEvidence
    from data.sources.tencent import quotes

    service = DecisionLoopService(store)
    portfolios = ModelPortfolios(service.store)
    archive = SettlementEvidence(service.store)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if now.hour < 16:
        return {"status": "pending_close"}
    capsule = service.capsule()
    if capsule:
        service.start_model_cohorts(capsule)
    sessions, _ = archive.pending_sessions(day)
    errors = []
    try:
        recovered = archive.recover(portfolios, through_day=day, cohorts=service, sessions=sessions,
                                    acquire=_historical_acquirer(archive))
    except Exception as exc:
        recovered = {}
        errors.append({"component": "historical_settlement", "error_category": type(exc).__name__})
    held = set(portfolios.symbols(include_targets=False))
    requested_symbols = sorted(sessions.get(day, set()) | held)
    archive.request(requested_symbols, day, held=held)
    # Newly enqueued rows become available after the loop's business timestamp.
    # Scope the claim to the session that the live quote endpoint can serve.
    work = archive.repo.claim("execution", trade_date=day, symbols=requested_symbols)
    facts = archive.facts(requested_symbols, day)
    symbols = [row["symbol"] for row in work
               if (row["symbol"], day) not in facts]
    acquired = {}
    diagnostics = {symbol: {} for symbol in symbols}
    for offset in range(0, len(symbols), 80):
        batch = symbols[offset:offset + 80]
        primary_error = None
        try:
            primary = quotes(batch)
        except Exception as exc:
            primary = {}
            primary_error = type(exc).__name__
            errors.append({"component": "tencent", "error_category": primary_error})
        for symbol in batch:
            fact, missing_fields = _execution_fact(symbol, day, primary.get(symbol, {}), "tencent")
            diagnostics[symbol]["tencent"] = missing_fields if primary.get(symbol) else [primary_error or "no_quote"]
            if fact:
                acquired[(symbol, day)] = fact
        missing = [symbol for symbol in batch if (symbol, day) not in acquired]
        if missing:
            from data.datahub import quotes as routed_quotes
            try:
                fallback = routed_quotes(missing)
                for symbol in missing:
                    quote = fallback.get(symbol, {})
                    fact, missing_fields = _execution_fact(symbol, day, quote, quote.get("provider") or "datahub")
                    diagnostics[symbol]["routed_quotes"] = missing_fields if quote else ["no_quote"]
                    if fact:
                        acquired[(symbol, day)] = fact
            except Exception as exc:
                errors.append({"component": "routed_quotes", "error_category": type(exc).__name__})
                for symbol in missing:
                    diagnostics[symbol]["routed_quotes"] = [type(exc).__name__]
    facts.update(archive.record_many(acquired))
    archive.repo.record_work_outcomes([
        (row, {"providers": diagnostics.get(row["symbol"], {})}, (row["symbol"], row["trade_date"]) in facts)
        for row in work])
    from application.reliability_jobs import refresh_corporate_evidence
    try:
        corporate = refresh_corporate_evidence(service.store, requested_symbols, day=day, now=now.isoformat())
    except Exception as exc:
        corporate = {"status": "failed", "error_category": type(exc).__name__}
        errors.append({"component": "corporate_evidence", **corporate})
    for key, fact in list(facts.items()):
        coverage = archive.repo.get("corporate_coverage", f"{key[1]}:{key[0]}")
        if coverage:
            fact = {k: v for k, v in fact.items() if k not in {"object_id", "revision", "fact_hash", "symbol"}}
            fact = {**fact, "corporate_coverage": coverage,
                    "corporate_action_unresolved": bool(coverage.get("gaps"))}
            facts[key] = archive.record(key[0], key[1], fact)
    settled = service.settle_models(facts, now=now.isoformat())
    model_books = portfolios.advance(facts, now=now.isoformat(),
                                     recorded_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
    from application.reliability_jobs import refresh_reliability
    try:
        # Historical revision replay can rebuild entire frozen selections and is
        # intentionally not part of eod_outcomes' 1800s settlement budget.  This
        # run still flushes evidence, reviews cases and reports the durable replay
        # queue for a separately budgeted reliability invocation.
        reliability = refresh_reliability(
            service.store, now=now.isoformat(), replay_limit=0,
        )
    except Exception as exc:
        reliability = {"status": "failed", "error_category": type(exc).__name__}
        errors.append({"component": "research_reviews", **reliability})
    remaining, calendar_unresolved = archive.pending_sessions(day)
    pending_day = min(remaining, default=None)
    waiting_books = sum(row.get("status") == "waiting_data" for row in model_books.values())
    pending_reason = ("original_execution_or_mark_facts_missing" if waiting_books
                      else "execution_evidence_or_settlement_pending" if pending_day
                      else "execution_calendar_unresolved" if calendar_unresolved else None)
    partial = bool(errors or len(facts) < len(requested_symbols) or pending_reason)
    return {"status": "partial" if partial else "complete", "errors": errors,
            "oldest_pending_date": pending_day, "pending_reason": pending_reason, "waiting_books": waiting_books,
            "settled": settled, "execution_fact_count": len(facts),
            "execution_queue": archive.repo.work_status("execution"), "requested_count": len(requested_symbols),
            "historical_execution_queue": archive.repo.work_status("historical_execution"),
            "recovered_original_sessions": recovered,
            "reliability": reliability, "corporate_evidence": corporate,
            "model_books": model_books,
            "quality_report": cached_quality(service.store)}


def weekly_research_cycle(store=None):
    """Evaluate registered DSL hypotheses on frozen data, never on hidden holdout.

    Current close-based diagnostics are exploration, not promotable execution
    evidence. That distinction is kept in every trial, including failed trials.
    """
    from analysis.factor_ast import PREREGISTERED_FACTORS, evaluate
    from application.decision_loop import DecisionLoopService
    from analysis.research_governance import HYPOTHESES
    from application.results import payload_hash
    import time
    deadline = time.monotonic() + 90
    service = DecisionLoopService(store)
    capsule = service.capsule()
    if not capsule or not capsule.get("manifest_id"):
        return {"status": "missing_formal_manifest"}
    symbols = [r["symbol"] for r in capsule["opportunity_set"]["top15"]]
    panel = service.store.load_daily_panel_from_manifest(capsule["manifest_id"], symbols=symbols)
    conn = service.store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT start_date,end_date FROM research_holdout_batches WHERE state IN ('sealed','evaluating')")
        sealed = list(cur.fetchall())
    finally:
        conn.close()
    if not panel.empty:
        for start, end in sealed:
            panel = panel[~panel["trade_date"].astype(str).between(str(start), str(end))]
    results = []
    financial = None
    flows = None
    for hypothesis, definition in HYPOTHESES.items():
        ast = PREREGISTERED_FACTORS[hypothesis]
        trial = service.register_trial(hypothesis_id=hypothesis, ast=ast,
                dataset_id=capsule["manifest_id"], data_class="reconstructed_history",
                code_revision=capsule.get("code_revision") or "unknown")
        if trial["state"] != "registered":
            results.append(trial)
            continue
        try:
            import pandas as pd
            fields = definition["fields"]
            source_panel = panel
            if hypothesis == "earnings_quality":
                financial = financial or service.store.load_financial_facts_from_manifest(capsule["manifest_id"])
                income, cashflow = financial.get("income", pd.DataFrame()), financial.get("cash_flow", pd.DataFrame())
                profit = next((c for c in ("np_parent_company_owners", "net_profit") if c in income), None)
                ocf = next((c for c in ("net_operate_cash_flow", "net_operating_cash_flow") if c in cashflow), None)
                if profit and ocf:
                    left = income.loc[income["symbol"].isin(symbols), ["symbol", "stat_date", "pub_date", profit]]
                    right = cashflow.loc[cashflow["symbol"].isin(symbols), ["symbol", "stat_date", "pub_date", ocf]]
                    source_panel = left.merge(right, on=["symbol", "stat_date"], suffixes=("_income", "_cash"))
                    source_panel["trade_date"] = source_panel[["pub_date_income", "pub_date_cash"]].astype(str).max(axis=1)
                    source_panel = source_panel.rename(columns={profit: "net_profit", ocf: "operating_cash_flow"})
                else:
                    source_panel = pd.DataFrame()
            elif hypothesis == "capital_persistence":
                flows = service.store.load_fund_flow_panel(capsule["market_as_of"], trading_days=20)
                source_panel = (flows[flows["symbol"].isin(symbols)].merge(
                    panel[["symbol", "trade_date", "amount"]], on=["symbol", "trade_date"], how="inner")
                    if not flows.empty and "amount" in panel else pd.DataFrame())
            if source_panel.empty or any(field not in source_panel for field in fields):
                raise ValueError("required_frozen_factor_fields_missing")
            diagnostics = {}
            for symbol, frame in source_panel.groupby("symbol"):
                if time.monotonic() >= deadline:
                    raise TimeoutError("weekly_research_time_budget")
                frame = frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
                # Missing sealed intervals are not bridged by lags: use only the
                # contiguous tail after the most recent hidden interval.
                # Future holdouts must not erase today's training data. When a
                # hidden interval has started, discard it and any earlier tail so
                # rolling windows cannot bridge the deliberately hidden gap.
                if sealed and not frame.empty:
                    latest = str(frame["trade_date"].max())
                    started = [(str(start), str(end)) for start, end in sealed if str(start) <= latest]
                    if started:
                        last_end = max(end for _, end in started)
                        frame = frame[frame["trade_date"].astype(str) > last_end]
                if frame.empty:
                    continue
                inputs = {field: [float(v) if pd.notna(v) else None for v in pd.to_numeric(frame[field], errors="coerce")] for field in fields}
                import os
                worker_image = os.getenv("RESEARCH_WORKER_IMAGE", "").strip()
                if worker_image:
                    from application.research_lab import run_isolated
                    values = run_isolated(ast, inputs, image=worker_image)["values"]
                else:
                    # Built-in validated arithmetic DSL only. No eval, generated
                    # Python, third-party model code or inherited worker secrets.
                    values = evaluate(ast, inputs)
                diagnostics[str(symbol)] = {"latest_factor": values[-1], "rows": len(values),
                                             "input_hash": payload_hash(inputs), "inputs": inputs,
                                             "dates": frame["trade_date"].astype(str).tolist()}
            if not diagnostics:
                raise ValueError("no_unsealed_training_rows")
            results.append(service.finish_trial(trial["trial_id"], diagnostics={
                "data_class": "reconstructed_history", "factor_values": diagnostics,
                "promotion_blocker": "requires_forward_net_evidence_and_fresh_holdout"}))
        except Exception as exc:
            results.append(service.finish_trial(trial["trial_id"], error_category=type(exc).__name__,
                           diagnostics={"reason": "required_frozen_data_or_formula_unavailable"}))
    return {"status": "complete", "trials": results}
