"""Immutable dated execution facts and fair bounded acquisition work."""
from collections import defaultdict
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from analysis.decision_evaluation import missing_execution_fields
from application.model_portfolios import dated_price, market_time, target_execution_at
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
        return self.record_many({(symbol, day): fact})[(symbol, day)]

    def record_many(self, records, *, historical=False):
        from data.corporate_actions import coverage_complete
        result = {}
        if not records:
            return result
        with self.repo.transaction() as cur:
            for (symbol, day), fact in records.items():
                if fact.get("trade_date") != day or fact.get("adjustment") != "raw" or not fact.get("provider"):
                    raise ValueError("invalid_execution_fact")
                fact = {**fact, "corporate_actions_complete": coverage_complete(fact.get("corporate_coverage"),
                    symbol=symbol, day=day, events=fact.get("corporate_actions") or [])}
                identity = f"{day}:{symbol}"
                old = self.repo.read(cur, "execution_fact", identity, "research")
                content = {**fact, "symbol": symbol, "fact_hash": payload_hash(fact)}
                if old and (old.get("fact_hash") == content["fact_hash"] or
                            historical and not missing_execution_fields(old, day) and dated_price(old, day, "close")
                            and (not old.get("suspended") or dated_price(old, day, "previous_close"))):
                    result[(symbol, day)] = old
                else:
                    result[(symbol, day)] = self.repo.append(cur, "execution_fact", identity, "research", content, old)
                if historical and not missing_execution_fields(result[(symbol, day)], day):
                    execution_id = payload_hash({"kind": "execution", "key": identity})
                    cur.execute("UPDATE research_reliability_work SET state='complete' WHERE work_id=? AND state='pending'",
                                (execution_id,))
        return result

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
                execution = target_execution_at(item, calendar)
                if not execution:
                    unresolved += 1
                    continue
                day = market_time(execution).date().isoformat()
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

    def _recovery_days(self, through_day):
        conn = self.repo.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT trade_date FROM research_trade_calendar WHERE trade_date<=? ORDER BY trade_date", (through_day,))
            calendar = [str(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT baseline,payload FROM research_model_targets WHERE state='pending'")
            earliest = {}
            for baseline, raw in cur.fetchall():
                target = json.loads(raw)
                if target.get("recording_mode") != "contemporaneous":
                    continue
                execution = target_execution_at(target, calendar)
                if execution:
                    day = market_time(execution).date().isoformat()
                    earliest[baseline] = min(earliest.get(baseline, through_day), day)
            cur.execute("SELECT baseline,payload FROM research_model_portfolios")
            candidates = set()
            for baseline, raw in cur.fetchall():
                ledger = json.loads(raw)
                marks = ledger["marks"]
                if baseline.startswith("policyarm:") and len(marks) >= 120:
                    continue
                next_day = earliest.get(baseline, through_day)
                blocker = ledger.get("settlement_blocker") or {}
                if blocker.get("stage") == "close_valuation":
                    next_day = min(next_day, blocker["trade_date"])
                if marks:
                    last_day = marks[-1]["trade_date"]
                    next_day = min(next_day, next((date for date in calendar if date > last_day), through_day))
                if next_day < through_day:
                    candidates.add(next_day)
            return candidates
        finally:
            conn.close()

    def recover(self, portfolios, *, through_day, cohorts=None, sessions=None, acquire=None):
        """Advance each book in trading-day order with bounded historical acquisition."""
        if sessions is None:
            sessions, _ = self.pending_sessions(through_day)
        result = {}
        recorded_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        historical = sorted(day for day in sessions if day < through_day)
        examined = set()
        initial_days = self._recovery_days(through_day)
        for _ in range(10):
            book_days = self._recovery_days(through_day)
            candidates = (book_days & initial_days) - examined or book_days - examined
            if not candidates:
                candidates = set(historical) - examined
            if not candidates:
                break
            day = min(candidates)
            examined.add(day)
            held = set(portfolios.symbols(include_targets=False))
            session_symbols = sessions.get(day, set())
            symbols = session_symbols | held
            facts = self.facts(symbols, day)
            if acquire:
                execution_symbols = symbols if session_symbols else set()
                wanted = {s for s in symbols if not dated_price(facts.get((s, day)), day, "close") or
                          (s in execution_symbols and missing_execution_fields(facts[(s, day)], day)) or
                          (s in held and facts[(s, day)].get("suspended")
                           and not dated_price(facts[(s, day)], day, "previous_close"))}
                facts.update(acquire(day, wanted))
            if facts or day in book_days:
                result[day] = portfolios.advance(facts, now=day + "T16:30:00+08:00", recorded_at=recorded_at)
                if cohorts is not None:
                    result[day]["cohorts_settled"] = cohorts.settle_models(facts, now=day + "T16:30:00+08:00")
        return result
