"""Immutable dated execution facts and fair bounded acquisition work."""
from data.reliability_store import ReliabilityStore
from application.results import payload_hash


class SettlementEvidence:
    def __init__(self, store):
        self.repo = ReliabilityStore(store)

    def request(self, symbols, day, *, held=()):
        for symbol in set(symbols):
            self.repo.enqueue("execution", f"{day}:{symbol}", {"symbol": symbol, "trade_date": day},
                              priority=0 if symbol in held else 1)

    def record(self, symbol, day, fact):
        if fact.get("trade_date") != day or fact.get("adjustment") != "raw" or not fact.get("provider"):
            raise ValueError("invalid_execution_fact")
        from data.corporate_actions import coverage_complete
        fact = {**fact, "corporate_actions_complete": coverage_complete(fact.get("corporate_coverage"),
            symbol=symbol, day=day, events=fact.get("corporate_actions") or [])}
        identity = f"{day}:{symbol}"
        old = self.repo.get("execution_fact", identity)
        content = {**fact, "symbol": symbol, "fact_hash": payload_hash(fact)}
        if old and old.get("fact_hash") == content["fact_hash"]:
            return old
        return self.repo.put("execution_fact", identity, content, expected_revision=(old or {}).get("revision", 0))

    def facts(self, symbols, day):
        result = {}
        for symbol in symbols:
            value = self.repo.get("execution_fact", f"{day}:{symbol}")
            if value:
                result[(symbol, day)] = value
        return result

    def paired_initial_state(self, ledger, *, old_policy, new_policy, at):
        from copy import deepcopy
        if old_policy == new_policy:
            raise ValueError("paired_policies_must_differ")
        initial = deepcopy(ledger)
        identity = payload_hash({"initial": initial, "policies": [old_policy, new_policy], "at": at})
        return self.repo.once("policy_pair", identity, {"at": at, "initial_state_hash": payload_hash(initial),
            "arms": {"hold": deepcopy(initial), "incumbent": deepcopy(initial), "proposed": deepcopy(initial)},
            "policies": {"incumbent": old_policy, "proposed": new_policy}, "status": "registered"})

    def recover(self, portfolios, *, through_day, cohorts=None):
        """Original facts only. Never fetch today's quote to fill a missed date."""
        import json
        conn = self.repo.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM research_model_targets WHERE state='pending'")
            dates = {str(json.loads(r[0]).get("earliest_execution_at") or "")[:10] for r in cur.fetchall()}
            cohort_symbols = set()
            if cohorts is not None:
                cur.execute("SELECT symbol,payload FROM research_model_orders WHERE state='pending'")
                for symbol, payload in cur.fetchall():
                    dates.add(str(json.loads(payload).get("earliest_execution_at") or "")[:10])
                    cohort_symbols.add(symbol)
        finally:
            conn.close()
        result = {}
        for day in sorted(d for d in dates if d and d < through_day)[:10]:
            symbols = sorted(set(portfolios.symbols()) | cohort_symbols)
            self.request(symbols, day)
            facts = self.facts(symbols, day)
            if facts:
                result[day] = portfolios.advance(facts, now=day + "T16:30:00+08:00")
                if cohorts is not None:
                    result[day]["cohorts_settled"] = cohorts.settle_models(facts, now=day + "T16:30:00+08:00")
        return result
