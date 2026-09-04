"""Bounded consumers on existing jobs; optional research never blocks selection."""
from datetime import datetime, timedelta
from data.reliability_store import ReliabilityStore


def refresh_corporate_evidence(store, symbols, *, day, now):
    import os
    from data.corporate_actions import fetch_dividends
    repo = ReliabilityStore(store)
    if not os.getenv("TUSHARE_TOKEN") and not os.getenv("TUSHARE_API_KEY"):
        return {"status": "unavailable", "reason": "provider_not_configured", "queried": 0}
    for symbol in symbols:
        repo.enqueue("corporate_actions", f"{day}:{symbol}", {"symbol": symbol, "day": day}, priority=1)
    rows = repo.claim("corporate_actions", limit=3, now=now)
    for row in rows:
        symbol = row["symbol"]
        start = (datetime.fromisoformat(row["day"]) - timedelta(days=30)).date().isoformat()
        try:
            evidence = fetch_dividends(symbol, start=start, end=row["day"], observed_at=now)
            repo.once("corporate_coverage", f"{row['day']}:{symbol}", evidence)
            for event in evidence.get("events", []):
                from application.research_cases import ResearchCases
                ResearchCases(store).event(symbol, {**event, "source_id": "tushare:dividend",
                    "published_at": now, "severity": "unknown", "title": "权益事件需要核实登记日持仓与税额"})
            repo.finish(row["work_id"])
        except Exception as exc:
            repo.once("corporate_failure", f"{row['work_id']}:{row['attempt']}",
                      {"symbol": symbol, "error_category": type(exc).__name__, "status": "FAILED"})
    return {"queried": len(rows), "queue": repo.work_status("corporate_actions"), "scope": "dividend_discovery_only"}


def refresh_reliability(store, *, now):
    from data.acquisition_evidence import flush
    from application.model_invocations import flush as flush_models
    from application.research_cases import ResearchCases
    cases = ResearchCases(store)
    conn = store.connect()
    try:
        cur = conn.cursor()
        cutoff = (datetime.fromisoformat(now) - timedelta(days=2)).isoformat()
        cur.execute("SELECT event_id,symbol,title,effective_at,source_origin,materiality,confirmation_status "
                    "FROM research_event_records WHERE effective_at>=? AND effective_at<=? ORDER BY effective_at DESC LIMIT 100",
                    (cutoff, now))
        events = cur.fetchall()
    finally:
        conn.close()
    for identity, symbol, title, published, source, materiality, confirmation in events:
        if cases.repo.get("case", str(symbol)):
            cases.event(str(symbol), {"event_id": "event:" + str(identity), "source_id": source or "event_registry",
                "published_at": str(published), "title": title or "新事件待复核",
                "severity": "major" if float(materiality or 0) >= .7 else "unknown" if confirmation != "confirmed" else "routine"})
    for impact in cases.repo.list("revision_impact", limit=100):
        if cases.repo.get("case", impact["symbol"]):
            from application.results import payload_hash
            cases.event(impact["symbol"], {"event_id": "revision:" + payload_hash(impact),
                "source_id": impact["new_dataset_id"], "published_at": now,
                "severity": "unknown", "title": "已引用财务数据出现修订，需要复核旧结论",
                "changed_fields": impact["changed_fields"]})
    from application.revision_replay import process_revision_impacts
    reviews = cases.review(now=now)
    return {"acquisition": flush(store), "model_invocations": flush_models(store),
            "revision_replays": process_revision_impacts(store, now=now),
            "case_reviews": reviews,
            "prediction_calibration": cases.settle_due(owner="portfolio-primary", now=now)}
