"""Immutable dated execution facts and fair bounded acquisition work."""
from bisect import bisect_right
from collections import defaultdict
import json

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
        keys = [f"{day}:{symbol}" for symbol in sorted(set(symbols))]
        result = {}
        if not keys:
            return result
        conn = self.repo.store.connect()
        try:
            cur = conn.cursor()
            for offset in range(0, len(keys), 500):
                batch = keys[offset:offset + 500]
                cur.execute(
                    "SELECT object_id,payload,revision FROM research_reliability_records "
                    "WHERE kind='execution_fact' AND owner_id='research' "
                    f"AND object_id IN ({','.join('?' for _ in batch)}) "
                    "ORDER BY revision", batch)
                for identity, payload, revision in cur.fetchall():
                    result[(identity[11:], day)] = {**json.loads(payload), "revision": revision}
            return result
        finally:
            conn.close()

    def pending_sessions(self, through_day):
        """Resolve the required original session from each frozen forward intent."""
        conn = self.repo.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT trade_date FROM research_trade_calendar ORDER BY trade_date")
            calendar = [str(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT symbol,payload FROM research_model_orders WHERE state='pending' "
                        "UNION ALL SELECT NULL,payload FROM research_model_targets WHERE state='pending'")
            sessions = defaultdict(set)
            unresolved = 0
            for symbol, raw in cur.fetchall():
                item = json.loads(raw)
                if item.get("recording_mode") != "contemporaneous":
                    continue
                day = (item.get("earliest_execution_at") or "")[:10]
                if not day:
                    index = bisect_right(calendar, item["published_at"][:10])
                    if index == len(calendar):
                        unresolved += 1
                        continue
                    day = calendar[index]
                if day <= through_day:
                    sessions[day].update([symbol] if symbol else item["weights"])
            return dict(sessions), unresolved
        finally:
            conn.close()

    def paired_initial_state(self, ledger, *, old_policy, new_policy, at):
        from copy import deepcopy
        if old_policy == new_policy:
            raise ValueError("paired_policies_must_differ")
        initial = deepcopy(ledger)
        identity = payload_hash({"initial": initial, "policies": [old_policy, new_policy], "at": at})
        return self.repo.once("policy_pair", identity, {"at": at, "initial_state_hash": payload_hash(initial),
            "arms": {"hold": deepcopy(initial), "incumbent": deepcopy(initial), "proposed": deepcopy(initial)},
            "policies": {"incumbent": old_policy, "proposed": new_policy}, "status": "registered"})

    def recover(self, portfolios, *, through_day, cohorts=None, sessions=None):
        """Replay archived original sessions in order, without enqueuing unserviceable dates."""
        if sessions is None:
            sessions, _ = self.pending_sessions(through_day)
        conn = self.repo.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT substr(object_id,1,10) FROM research_reliability_records "
                        "WHERE kind='execution_fact' AND owner_id='research' AND substr(object_id,1,10)<?",
                        (through_day,))
            archived_days = {str(row[0]) for row in cur.fetchall()}
        finally:
            conn.close()
        result = {}
        for day in sorted(archived_days.intersection(sessions))[:10]:
            symbols = sessions[day] | set(portfolios.symbols(include_targets=False))
            facts = self.facts(symbols, day)
            if facts:
                result[day] = portfolios.advance(facts, now=day + "T16:30:00+08:00")
                if cohorts is not None:
                    result[day]["cohorts_settled"] = cohorts.settle_models(facts, now=day + "T16:30:00+08:00")
        return result
