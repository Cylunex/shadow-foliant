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
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482718)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT payload FROM research_model_portfolios WHERE baseline='fusion'")
            previous_ledger = cur.fetchone()
            initial_state = json.loads(previous_ledger[0]) if previous_ledger else empty_ledger()
            cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='policy_arm_candidates'",
                        (capsule["run_id"],))
            arm_row = cur.fetchone()
            arm_metadata = json.loads(arm_row[0]) if arm_row else {}
            for baseline, rows in {**groups, "low_turnover": groups.get("fusion", [])}.items():
                if not rows:
                    continue
                if baseline.startswith("policyarm:"):
                    cur.execute("SELECT payload FROM research_model_portfolios WHERE baseline=?", (baseline,))
                    prior_arm = cur.fetchone()
                    if prior_arm and len(json.loads(prior_arm[0])["marks"]) >= 120:
                        continue
                selected = datetime.fromisoformat(capsule["published_at"])
                week = selected.strftime("%G-%V")
                if baseline == "low_turnover":
                    cur.execute("SELECT payload FROM research_model_targets WHERE baseline=? ORDER BY created_at DESC LIMIT 1", (baseline,))
                    previous = cur.fetchone()
                    if previous and json.loads(previous[0]).get("week") == week:
                        continue
                target = {"capsule_id": capsule["capsule_id"], "baseline": baseline, "week": week,
                          "policy_hash": arm_metadata.get(baseline, {}).get("policy_hash", capsule.get("policy_hash")),
                          "published_at": capsule["published_at"], "recording_mode": "contemporaneous" if forward else "backfilled",
                          "recorded_at": recorded_at,
                          "earliest_execution_at": capsule["earliest_execution_at"],
                          "weights": {r["symbol"]: 1 / len(rows) for r in rows}}
                identity = payload_hash({"capsule_id": capsule["capsule_id"], "baseline": baseline})
                cur.execute("INSERT INTO research_model_targets (target_id,baseline,run_id,state,payload,created_at) "
                            "VALUES (?,?,?,?,?,?) ON CONFLICT(target_id) DO NOTHING",
                            (identity, baseline, capsule["run_id"], "pending", json.dumps(target), recorded_at))
                ledger = empty_ledger()
                if baseline.startswith("policyarm:"):
                    from copy import deepcopy
                    ledger = deepcopy(initial_state)
                    ledger["paired_initial_state_hash"] = payload_hash(initial_state)
                    ledger["paired_started_at"] = recorded_at
                    latest = ledger["marks"][-1] if ledger["marks"] else {}
                    ledger["paired_initial_verified"] = latest.get("status") == "verified" or not ledger["positions"]
                    if latest.get("net_asset_value"):
                        ledger["initial_cash"] = latest["net_asset_value"]
                    ledger["marks"] = []
                    ledger["policy_transition"] = False
                cur.execute("INSERT INTO research_model_portfolios (baseline,payload,updated_at) VALUES (?,?,?) "
                            "ON CONFLICT(baseline) DO NOTHING", (baseline, json.dumps(ledger), capsule["published_at"]))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def symbols(self, limit=None):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM research_model_portfolios")
            books = [json.loads(r[0]) for r in cur.fetchall()]
            symbols = {s for book in books if not (book.get("paired_started_at") and len(book["marks"]) >= 120)
                       for s, p in book["positions"].items() if p["quantity"]}
            symbols.update(r["symbol"] for book in books for r in book.get("receivables", {}).values()
                           if r["state"] == "receivable")
            cur.execute("SELECT payload FROM research_model_targets WHERE state='pending' ORDER BY created_at")
            for row in cur.fetchall():
                symbols.update(json.loads(row[0])["weights"])
            return sorted(symbols)[:limit] if limit is not None else sorted(symbols)
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
                if baseline.startswith("policyarm:") and len(ledger["marks"]) >= 120:
                    continue
                if ledger["marks"] and ledger["marks"][-1]["trade_date"] >= day:
                    continue
                unresolved = False
                entitled = {s for s, p in ledger["positions"].items() if p["quantity"]}
                entitled.update(r["symbol"] for r in ledger.get("receivables", {}).values() if r["state"] == "receivable")
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
                    if target["recording_mode"] != "contemporaneous":
                        state, reason = "expired", "target_registered_after_execution"
                    elif earliest[:10] < day:
                        # A scheduler delay is not a late target. Preserve intent
                        # until original dated facts are replayed, never use today.
                        unresolved = True
                        break
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
                            unresolved = True
                            break
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
                            from analysis.execution_scenarios import execution_scenarios
                            target["execution_stress"] = [
                                {"symbol": symbol, "scenarios": execution_scenarios(
                                    {"side": "buy", "quantity": quantities.get(symbol, 0),
                                     "published_at": target["published_at"], "earliest_execution_at": earliest},
                                    current[symbol], ExecutionRules(**current[symbol]["execution_rules"]), cash=nav)}
                                for symbol in sorted(target["weights"])[:15]]
                            old_policy = ledger.get("policy_hash")
                            if old_policy and old_policy != target.get("policy_hash") and not baseline.startswith("policyarm:"):
                                ledger["policy_transition"] = True
                                ledger.setdefault("policy_history", []).append({"from": old_policy,
                                    "to": target.get("policy_hash"), "at": day})
                            ledger["policy_hash"] = target.get("policy_hash")
                            state, reason = "processed", "shared_next_open_model"
                    target.update(state=state, reason=reason)
                    cur.execute("UPDATE research_model_targets SET state=?,payload=? WHERE target_id=? AND state='pending'",
                                (state, json.dumps(target), identity))
                held = {s for s, p in ledger["positions"].items() if p["quantity"]}
                if unresolved or any((s, day) not in facts for s in held):
                    cur.execute("UPDATE research_model_portfolios SET payload=?,updated_at=? WHERE baseline=?",
                                (json.dumps(ledger), now, baseline))
                    output[baseline] = {"status": "waiting_data", "trade_date": day,
                                        "reason": "original_execution_or_mark_facts_missing"}
                    continue
                for symbol in entitled:
                    for event in facts.get((symbol, day), {}).get("corporate_actions") or []:
                        ledger = apply_corporate_action(ledger, event)
                prices = {s: facts[(s, day)]["close"] for s in held if (s, day) in facts and facts[(s, day)].get("close")}
                complete = all(facts.get((s, day), {}).get("corporate_actions_complete") for s in held)
                complete = complete and not ledger.get("policy_transition")
                complete = complete and ledger.get("paired_initial_verified", True)
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
