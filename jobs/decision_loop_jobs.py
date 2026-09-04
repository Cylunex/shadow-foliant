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


def daily_decision_loop(store=None):
    from application.decision_loop import DecisionLoopService
    from analysis.decision_evaluation import equity_rules
    from data.sources.tencent import quotes
    from data.valuation_contract import closing_timestamp
    service = DecisionLoopService(store)
    from application.model_portfolios import ModelPortfolios
    portfolios = ModelPortfolios(service.store)
    from application.settlement_evidence import SettlementEvidence
    archive = SettlementEvidence(service.store)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    day = now.date().isoformat()
    if now.hour < 16:
        return {"status": "pending_close"}
    capsule = service.capsule()
    if capsule:
        service.start_model_cohorts(capsule)
    conn = service.store.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT symbol FROM research_model_orders WHERE state='pending' "
                    "ORDER BY symbol")
        symbols = [str(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
    symbols = sorted(set(symbols) | set(portfolios.symbols()))
    archive.request(symbols, day, held=portfolios.symbols())
    work = archive.repo.claim("execution", now=now.isoformat())
    errors = []
    # Archived historical facts close the work item without spending another
    # provider request. A current quote is never a substitute for an older date.
    for row in work:
        if archive.facts([row["symbol"]], row["trade_date"]):
            archive.repo.finish(row["work_id"])
    requested_symbols = symbols
    symbols = [r["symbol"] for r in work if r["trade_date"] == day]
    snapshot = {}
    for offset in range(0, min(160, len(symbols)), 80):
        batch = symbols[offset:offset + 80]
        try:
            primary = quotes(batch)
        except Exception as exc:
            primary = {}
            errors.append({"component": "tencent", "error_category": type(exc).__name__})
        snapshot.update({symbol: {**quote, "execution_provider": "tencent"} for symbol, quote in primary.items()})
        missing = [symbol for symbol in batch if symbol not in primary]
        if missing:
            # Reuse configured stable-first routing. Only fully dated raw quotes
            # with limits/volume can become execution facts below; no invented
            # 10% price limits or yesterday-close fills for incomplete fallbacks.
            from data.datahub import quotes as routed_quotes
            try:
                for symbol, quote in routed_quotes(missing).items():
                    snapshot[symbol] = {**quote, "execution_provider": quote.get("provider") or "datahub"}
            except Exception as exc:
                errors.append({"component": "routed_quotes", "error_category": type(exc).__name__})
    facts = {}
    for symbol, quote in snapshot.items():
        timestamp = closing_timestamp(quote.get("quote_time") or quote.get("observed_at"), day)
        rules = equity_rules(symbol)
        if not timestamp or rules is None or any(quote.get(field) is None for field in ("open", "price", "volume", "limit_up", "limit_down")):
            continue
        facts[(symbol, day)] = {
            "trade_date": day, "adjustment": "raw", "open": quote.get("open"),
            "close": quote.get("price"), "volume": quote.get("volume"),
            "limit_up": quote.get("limit_up"), "limit_down": quote.get("limit_down"),
            "suspended": not bool(quote.get("volume")), "provider": quote["execution_provider"],
            "observed_at": timestamp, "execution_rules": asdict(rules)}
        archive.record(symbol, day, facts[(symbol, day)])
    for row in work:
        if (row["symbol"], row["trade_date"]) in facts:
            archive.repo.finish(row["work_id"])
    facts.update(archive.facts(requested_symbols, day))
    recovered = archive.recover(portfolios, through_day=day, cohorts=service)
    from application.reliability_jobs import refresh_corporate_evidence
    try:
        corporate = refresh_corporate_evidence(service.store, requested_symbols, day=day, now=now.isoformat())
    except Exception as exc:
        corporate = {"status": "failed", "error_category": type(exc).__name__}
        errors.append({"component": "corporate_evidence", **corporate})
    for key, fact in list(facts.items()):
        coverage = archive.repo.get("corporate_coverage", f"{key[1]}:{key[0]}")
        if coverage:
            fact = {**fact, "corporate_coverage": coverage,
                    "corporate_action_unresolved": bool(coverage.get("gaps"))}
            facts[key] = archive.record(key[0], key[1], fact)
    settled = service.settle_models(facts, now=now.isoformat())
    model_books = portfolios.advance(facts, now=now.isoformat())
    from application.reliability_jobs import refresh_reliability
    try:
        reliability = refresh_reliability(service.store, now=now.isoformat())
    except Exception as exc:
        reliability = {"status": "failed", "error_category": type(exc).__name__}
        errors.append({"component": "research_reviews", **reliability})
    return {"status": "partial" if errors or len(facts) < len(requested_symbols) else "complete", "errors": errors,
            "settled": settled, "execution_fact_count": len(facts),
            "execution_queue": archive.repo.work_status("execution"), "requested_count": len(requested_symbols),
            "recovered_original_sessions": recovered,
            "reliability": reliability, "corporate_evidence": corporate,
            "model_books": model_books,
            "quality_report": refresh_quality(service.store)}


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
