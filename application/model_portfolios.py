"""Persistent comparable full-cash model accounts, separate from real holdings."""
from bisect import bisect_right
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal
import json
from application.results import payload_hash
from analysis.decision_evaluation import ExecutionRules, missing_execution_fields, money, simulate_fill, unit_price
from analysis.model_ledger import empty_ledger, apply_fill, apply_corporate_action, mark_ledger


def market_time(value):
    timestamp = datetime.fromisoformat(value)
    zone = ZoneInfo("Asia/Shanghai")
    return timestamp.astimezone(zone) if timestamp.tzinfo else timestamp.replace(tzinfo=zone)


def target_execution_at(target, calendar):
    if target.get("earliest_execution_at"):
        return target["earliest_execution_at"]
    index = bisect_right(calendar, market_time(target["published_at"]).date().isoformat())
    return f"{calendar[index]}T09:30:00+08:00" if index < len(calendar) else None


def dated_price(fact, day, field):
    if fact and fact.get("trade_date") == day and fact.get("adjustment") == "raw":
        try:
            return unit_price(fact.get(field))
        except (ValueError, TypeError, ArithmeticError):
            pass
    return None


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
            cur.execute("SELECT payload FROM selection_artifacts WHERE run_id=? AND artifact_type='fusion_policy'",
                        (capsule["run_id"],))
            policy_row = cur.fetchone()
            fusion_hash = payload_hash(json.loads(policy_row[0])) if policy_row else None
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
                if fusion_hash:
                    target["fusion_policy_hash"] = arm_metadata.get(baseline, {}).get("policy_hash") or fusion_hash
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
                    ledger["paired_initial_reconstructed"] = latest.get("data_class") == "reconstructed_history"
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

    def symbols(self, limit=None, *, include_targets=True):
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM research_model_portfolios")
            books = [json.loads(r[0]) for r in cur.fetchall()]
            symbols = {s for book in books if not (book.get("paired_started_at") and len(book["marks"]) >= 120)
                       for s, p in book["positions"].items() if p["quantity"]}
            symbols.update(r["symbol"] for book in books for r in book.get("receivables", {}).values()
                           if r["state"] == "receivable")
            if include_targets:
                cur.execute("SELECT payload FROM research_model_targets WHERE state='pending'")
                for row in cur.fetchall():
                    symbols.update(json.loads(row[0])["weights"])
            return sorted(symbols)[:limit] if limit is not None else sorted(symbols)
        finally:
            conn.close()

    def advance(self, facts, *, now, recorded_at=None):
        """One transaction advances all baselines under the same execution facts.

        A missed session is never filled at a later price. Unknown corporate
        actions leave NAV indicative and exclude it from promotion evidence.
        """
        conn = self.store.connect()
        timestamp = market_time(now)
        day = timestamp.date().isoformat()
        updated = recorded_at or now
        output = {}
        try:
            cur = conn.cursor()
            if self.store._is_postgres:
                cur.execute("SELECT pg_advisory_xact_lock(1936482718)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT trade_date FROM research_trade_calendar ORDER BY trade_date")
            calendar = [str(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT baseline,payload FROM research_model_portfolios ORDER BY baseline")
            for baseline, raw in cur.fetchall():
                ledger = json.loads(raw)
                if baseline.startswith("policyarm:") and len(ledger["marks"]) >= 120:
                    continue
                if ledger["marks"] and ledger["marks"][-1]["trade_date"] >= day:
                    continue
                if ledger.get("last_execution_date", "") > day:
                    continue
                blocker = None
                reconstructed = (ledger.get("reconstructed_execution_date") == day
                                 or ledger.get("paired_initial_reconstructed", False))
                entitled = {s for s, p in ledger["positions"].items() if p["quantity"]}
                entitled.update(r["symbol"] for r in ledger.get("receivables", {}).values() if r["state"] == "receivable")
                cur.execute("SELECT target_id,payload FROM research_model_targets WHERE baseline=? AND state='pending' "
                            "ORDER BY created_at,target_id", (baseline,))
                targets = [(identity, json.loads(raw)) for identity, raw in cur.fetchall()]
                if not ledger["marks"] and targets and market_time(targets[0][1]["published_at"]).date().isoformat() > day:
                    continue
                pending = []
                for identity, target in targets:
                    earliest = target_execution_at(target, calendar)
                    if not earliest or market_time(earliest) > timestamp:
                        continue
                    pending.append((market_time(earliest).isoformat(), identity, target))
                last_day = ledger["marks"][-1]["trade_date"] if ledger["marks"] else None
                prior_mark_day = next((d for d in calendar if last_day and last_day < d < day), None)
                last_blocker = ledger.get("settlement_blocker") or {}
                if last_blocker.get("stage") == "close_valuation" and last_blocker["trade_date"] < day:
                    prior_mark_day = min(prior_mark_day or day, last_blocker["trade_date"])
                pending.sort(key=lambda row: row[0])
                if prior_mark_day and (not pending or prior_mark_day < pending[0][0][:10]):
                    previous = ledger.get("settlement_blocker") or {}
                    blocker = (previous if previous.get("trade_date") == prior_mark_day else
                               {"stage": "earlier_close_required", "trade_date": prior_mark_day})
                for earliest, identity, target in pending if not blocker else ():
                    original_day = earliest[:10]
                    context = {"target_id": identity, "trade_date": original_day}
                    if target["recording_mode"] != "contemporaneous":
                        state, reason = "expired", "target_registered_after_execution"
                    elif original_day < day:
                        previous = ledger.get("settlement_blocker") or {}
                        blocker = (previous if previous.get("target_id") == identity
                                   and previous.get("trade_date") == original_day else
                                   {"stage": "original_session_required", **context})
                        break
                    else:
                        wanted = set(target["weights"]) | {s for s, p in ledger["positions"].items() if p["quantity"]}
                        current = {symbol: facts.get((symbol, day)) for symbol in wanted}
                        gaps = {symbol: missing for symbol, fact in sorted(current.items())
                                if (missing := missing_execution_fields(fact, day))}
                        if gaps:
                            blocker = {"stage": "execution_evidence", "missing": gaps, **context}
                            break
                        # A suspended holding contributes to position sizing
                        # at the reference price already known at the open.
                        missing_suspended_marks = {
                            s: ["previous_close"] for s, p in ledger["positions"].items() if p["quantity"]
                            and current[s].get("suspended") and not dated_price(current[s], day, "previous_close")
                        }
                        if missing_suspended_marks:
                            blocker = {"stage": "execution_evidence", "missing": missing_suspended_marks, **context}
                            break
                        else:
                            if "fusion_policy_hash" not in target:
                                cur.execute("SELECT a.payload FROM selection_artifacts a JOIN research_model_targets t "
                                            "ON a.run_id=t.run_id WHERE t.target_id=? AND a.artifact_type='fusion_policy'",
                                            (identity,))
                                policy_row = cur.fetchone()
                                if policy_row:
                                    target["fusion_policy_hash"] = (target.get("policy_hash") if baseline.startswith("policyarm:")
                                                                    else payload_hash(json.loads(policy_row[0])))
                            if any(fact.get("data_class") == "reconstructed_history" for fact in current.values()):
                                reconstructed = True
                                ledger["reconstructed_execution_date"] = day
                            for symbol, fact in current.items():
                                for event in fact.get("corporate_actions") or []:
                                    ledger = apply_corporate_action(ledger, event)
                            nav = money(ledger["cash"]) + sum(
                                (dated_price(current[s], day, "previous_close") if current[s].get("suspended")
                                 else unit_price(current[s]["open"])) * p["quantity"]
                                for s, p in ledger["positions"].items() if p["quantity"])
                            quantities = {}
                            for symbol, weight in target["weights"].items():
                                if current[symbol].get("suspended"):
                                    quantities[symbol] = ledger["positions"].get(symbol, {}).get("quantity", 0)
                                    continue
                                rule = ExecutionRules(**current[symbol]["execution_rules"])
                                qty = int(nav * Decimal(str(weight)) / unit_price(current[symbol]["open"])) // rule.buy_step * rule.buy_step
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
                                    fills.append({"symbol": symbol, "side": side, "event_id": event_id,
                                                  "fact_hash": current[symbol].get("fact_hash"), **fill})
                            target["fills"] = fills
                            ledger["last_execution_date"] = day
                            ledger["last_execution_target_id"] = identity
                            target["suspended_symbols"] = sorted(s for s in wanted if current[s].get("suspended"))
                            from analysis.execution_scenarios import execution_scenarios
                            target["execution_stress"] = [
                                {"symbol": symbol, "scenarios": execution_scenarios(
                                    {"side": "buy", "quantity": quantities.get(symbol, 0),
                                     "published_at": target["published_at"], "earliest_execution_at": earliest},
                                    current[symbol], ExecutionRules(**current[symbol]["execution_rules"]), cash=nav)}
                                for symbol in sorted(target["weights"])[:15] if not current[symbol].get("suspended")]
                            old_policy = ledger.get("policy_hash")
                            old_fusion = ledger.get("fusion_policy_hash")
                            fusion = target.get("fusion_policy_hash")
                            if ((old_policy and old_policy != target.get("policy_hash")) or
                                    (old_fusion and old_fusion != fusion)) and not baseline.startswith("policyarm:"):
                                ledger["policy_transition"] = True
                                ledger.setdefault("policy_history", []).append({"from": old_policy,
                                    "to": target.get("policy_hash"), "at": day})
                            ledger["policy_hash"] = target.get("policy_hash")
                            if fusion:
                                ledger["fusion_policy_hash"] = fusion
                            else:
                                ledger.pop("fusion_policy_hash", None)
                            state, reason = "processed", "shared_next_open_model"
                    target.update(state=state, reason=reason)
                    cur.execute("UPDATE research_model_targets SET state=?,payload=? WHERE target_id=? AND state='pending'",
                                (state, json.dumps(target), identity))
                if not blocker:
                    for symbol in entitled:
                        for event in facts.get((symbol, day), {}).get("corporate_actions") or []:
                            ledger = apply_corporate_action(ledger, event)
                held = {s for s, p in ledger["positions"].items() if p["quantity"]}
                prices = {s: dated_price(facts.get((s, day)), day, "close") for s in sorted(held)}
                missing_closes = {s: ["close"] for s, price in prices.items() if price is None}
                if not blocker and missing_closes:
                    blocker = {"stage": "close_valuation", "trade_date": day, "missing": missing_closes}
                    if ledger.get("last_execution_date") == day:
                        blocker["target_id"] = ledger["last_execution_target_id"]
                if blocker:
                    ledger["settlement_blocker"] = blocker
                    cur.execute("UPDATE research_model_portfolios SET payload=?,updated_at=? WHERE baseline=?",
                                (json.dumps(ledger), updated, baseline))
                    output[baseline] = {"status": "waiting_data", "trade_date": day,
                                        "blocker": blocker,
                                        "reason": "original_execution_or_mark_facts_missing"}
                    continue
                complete = all(facts.get((s, day), {}).get("corporate_actions_complete") for s in held | entitled)
                complete = complete and ledger.get("paired_initial_verified", True)
                ledger.pop("settlement_blocker", None)
                ledger = mark_ledger(ledger, trade_date=day, prices=prices, corporate_actions_complete=complete,
                                     reconstructed=any(facts.get((s, day), {}).get("data_class") == "reconstructed_history"
                                                       for s in held | entitled) or reconstructed)
                cur.execute("UPDATE research_model_portfolios SET payload=?,updated_at=? WHERE baseline=?",
                            (json.dumps(ledger), updated, baseline))
                output[baseline] = ledger["marks"][-1]
            conn.commit()
            return output
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
