"""Persistent comparable full-cash model accounts, separate from real holdings."""
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import json
from application.results import payload_hash
from analysis.decision_evaluation import ExecutionRules, money, simulate_fill
from analysis.model_ledger import empty_ledger, apply_fill, apply_corporate_action, mark_ledger


class ModelPortfolios:
    def __init__(self, store):
        self.store = store

    def publish(self, capsule, groups):
        recorded_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
        deadline = capsule.get("earliest_execution_at")
        forward = (capsule["recording_mode"] == "contemporaneous"
                   and datetime.fromisoformat(recorded_at) >= datetime.fromisoformat(capsule["published_at"]) and
                   (datetime.fromisoformat(recorded_at) < datetime.fromisoformat(deadline)
                    if deadline else recorded_at[:10] == capsule["published_at"][:10]))
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            for baseline, rows in {**groups, "low_turnover": groups.get("fusion", [])}.items():
                if not rows:
                    continue
                selected = datetime.fromisoformat(capsule["published_at"])
                week = selected.strftime("%G-%V")
                if baseline == "low_turnover":
                    cur.execute("SELECT payload FROM research_model_targets WHERE baseline=? ORDER BY created_at DESC LIMIT 1", (baseline,))
                    previous = cur.fetchone()
                    if previous and json.loads(previous[0]).get("week") == week:
                        continue
                target = {"capsule_id": capsule["capsule_id"], "baseline": baseline, "week": week,
                          "policy_hash": capsule.get("policy_hash"),
                          "published_at": capsule["published_at"], "recording_mode": "contemporaneous" if forward else "backfilled",
                          "recorded_at": recorded_at,
                          "earliest_execution_at": capsule["earliest_execution_at"],
                          "weights": {r["symbol"]: 1 / len(rows) for r in rows}}
                identity = payload_hash({"capsule_id": capsule["capsule_id"], "baseline": baseline})
                cur.execute("INSERT INTO research_model_targets (target_id,baseline,run_id,state,payload,created_at) "
                            "VALUES (?,?,?,?,?,?) ON CONFLICT(target_id) DO NOTHING",
                            (identity, baseline, capsule["run_id"], "pending", json.dumps(target), recorded_at))
                ledger = empty_ledger()
                cur.execute("INSERT INTO research_model_portfolios (baseline,payload,updated_at) VALUES (?,?,?) "
                            "ON CONFLICT(baseline) DO NOTHING", (baseline, json.dumps(ledger), capsule["published_at"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def symbols(self, limit=160):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM research_model_portfolios")
            symbols = {s for r in cur.fetchall() for s, p in json.loads(r[0])["positions"].items() if p["quantity"]}
            cur.execute("SELECT payload FROM research_model_targets WHERE state='pending' ORDER BY created_at DESC LIMIT 30")
            for row in cur.fetchall():
                symbols.update(json.loads(row[0])["weights"])
            return sorted(symbols)[:limit]
        finally:
            conn.close()

    def advance(self, facts, *, now):
        """One transaction advances all baselines under the same execution facts.

        A missed session is never filled at a later price. Unknown corporate
        actions leave NAV indicative and exclude it from promotion evidence.
        """
        conn = self.store.connect()
        day = now[:10]
        output = {}
        try:
            cur = conn.cursor()
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482718)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT baseline,payload FROM research_model_portfolios ORDER BY baseline")
            for baseline, raw in cur.fetchall():
                ledger = json.loads(raw)
                if ledger["marks"] and ledger["marks"][-1]["trade_date"] >= day:
                    continue
                for symbol, position in list(ledger["positions"].items()):
                    if position["quantity"]:
                        for event in facts.get((symbol, day), {}).get("corporate_actions") or []:
                            ledger = apply_corporate_action(ledger, event)
                cur.execute("SELECT target_id,payload FROM research_model_targets WHERE baseline=? AND state='pending' "
                            "ORDER BY created_at", (baseline,))
                for identity, payload in cur.fetchall():
                    target = json.loads(payload)
                    earliest = target.get("earliest_execution_at")
                    if not earliest:
                        cur.execute("SELECT MIN(trade_date) FROM research_trade_calendar WHERE trade_date>?", (target["published_at"][:10],))
                        next_date = cur.fetchone()[0]
                        earliest = f"{next_date}T09:30:00+08:00" if next_date else None
                    if not earliest or earliest[:10] > day:
                        continue
                    if earliest[:10] < day or target["recording_mode"] != "contemporaneous":
                        state, reason = "expired", "missed_session_or_backfill"
                    else:
                        wanted = set(target["weights"]) | {s for s, p in ledger["positions"].items() if p["quantity"]}
                        current = {symbol: facts.get((symbol, day)) for symbol in wanted}
                        try:
                            valid = all(f and money(f.get("open")) > 0 and f.get("execution_rules") for f in current.values())
                            if valid:
                                for fact in current.values():
                                    ExecutionRules(**fact["execution_rules"])
                        except (ValueError, TypeError, ArithmeticError):
                            valid = False
                        if not valid:
                            state, reason = "unfilled", "required_execution_facts_missing"
                        else:
                            for symbol, fact in current.items():
                                for event in fact.get("corporate_actions") or []:
                                    ledger = apply_corporate_action(ledger, event)
                            nav = money(ledger["cash"]) + sum(
                                money(current[s]["open"]) * p["quantity"] for s, p in ledger["positions"].items() if p["quantity"])
                            quantities = {}
                            for symbol, weight in target["weights"].items():
                                rule = ExecutionRules(**current[symbol]["execution_rules"])
                                qty = int(nav * Decimal(str(weight)) / money(current[symbol]["open"])) // rule.buy_step * rule.buy_step
                                quantities[symbol] = qty if qty >= rule.min_buy else 0
                            fills = []
                            for side in ("sell", "buy"):
                                for symbol in sorted(wanted):
                                    position = ledger["positions"].get(symbol, {"quantity": 0, "last_buy_date": None})
                                    difference = quantities.get(symbol, 0) - position["quantity"]
                                    if side == "sell" and difference >= 0 or side == "buy" and difference <= 0:
                                        continue
                                    order = {"side": side, "quantity": abs(difference), "published_at": target["published_at"],
                                             "earliest_execution_at": earliest}
                                    rules = ExecutionRules(**current[symbol]["execution_rules"])
                                    sellable = position["quantity"] if not rules.t_plus or position["last_buy_date"] != day else 0
                                    fill = simulate_fill(order, current[symbol], rules, cash=ledger["cash"], sellable=sellable)
                                    event_id = payload_hash({"target": identity, "symbol": symbol, "side": side})
                                    ledger = apply_fill(ledger, event_id=event_id, symbol=symbol, side=side, fill=fill)
                                    fills.append({"symbol": symbol, "side": side, **fill})
                            target["fills"] = fills
                            ledger["policy_hash"] = target.get("policy_hash")
                            state, reason = "processed", "shared_next_open_model"
                    target.update(state=state, reason=reason)
                    cur.execute("UPDATE research_model_targets SET state=?,payload=? WHERE target_id=? AND state='pending'",
                                (state, json.dumps(target), identity))
                held = {s for s, p in ledger["positions"].items() if p["quantity"]}
                prices = {s: facts[(s, day)]["close"] for s in held if (s, day) in facts and facts[(s, day)].get("close")}
                complete = all(facts.get((s, day), {}).get("corporate_actions_complete") for s in held)
                ledger = mark_ledger(ledger, trade_date=day, prices=prices, corporate_actions_complete=complete)
                cur.execute("UPDATE research_model_portfolios SET payload=?,updated_at=? WHERE baseline=?",
                            (json.dumps(ledger), now, baseline))
                output[baseline] = ledger["marks"][-1]
            conn.commit()
            return output
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
