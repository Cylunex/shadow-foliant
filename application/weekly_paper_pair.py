"""Prospective one-session, equal-cash research pairs using archived execution facts."""
from dataclasses import asdict
from datetime import datetime, time
from analysis.decision_evaluation import ExecutionRules, money, simulate_fill, unit_price
from analysis.model_ledger import apply_fill, empty_ledger, mark_ledger
from application.market_prices import SHANGHAI, shanghai_instant
from application.results import payload_hash
from data.reliability_store import ReliabilityStore

KIND = "losna_weekly_paper_pair"
COST_FIELDS = ("commission_rate", "minimum_commission", "sell_tax_rate", "transfer_rate",
               "slippage_bps", "volume_participation")


def freeze_pair(store, batch, capsule, *, clock=lambda: datetime.now(SHANGHAI)):
    if (not batch or batch.get("run_id") != str(capsule.get("run_id"))
            or batch.get("manifest_id") != capsule.get("manifest_id") or batch.get("state") not in {"awaiting_window", "awaiting_labels"}
            or not batch.get("control_symbols") or not batch.get("reviews")
            or capsule.get("recording_mode") != "contemporaneous"):
        return None
    opening = shanghai_instant(capsule.get("earliest_execution_at"))
    publication = shanghai_instant(capsule.get("published_at"))
    repository = ReliabilityStore(store)
    with repository.transaction() as cur:
        existing = repository.read(cur, KIND, batch["run_id"], "research")
        if existing:
            return existing
        recorded = clock().astimezone(SHANGHAI)
        if not opening or not publication or not publication <= recorded < opening or opening.time() != time(9, 30):
            return None
        target = {"run_id": batch["run_id"], "packet_id": batch["packet_id"],
                  "capsule_id": capsule["capsule_id"], "manifest_id": batch["manifest_id"],
                  "recorded_at": recorded.isoformat(), "entry_at": opening.isoformat(),
                  "initial_cash": "100000.00", "cost_policy": {k: getattr(ExecutionRules(), k) for k in COST_FIELDS},
                  "model": "equal-cash-next-open-close-v1",
                  "blind": sorted(r["symbol"] for r in batch["reviews"]),
                  "formal": sorted(batch["control_symbols"])}
        return repository.append(cur, KIND, batch["run_id"], "research", target)


def freeze_latest_pair():
    from application.decision_loop import DecisionLoopService
    from application.weekly_contribution import weekly_contribution
    service = DecisionLoopService()
    capsule = service.capsule()
    if not capsule or not capsule.get("earliest_execution_at"):
        return None
    now = datetime.now(SHANGHAI)
    opening = shanghai_instant(capsule["earliest_execution_at"])
    if not opening or now >= opening or capsule.get("recording_mode") != "contemporaneous":
        return None
    existing = ReliabilityStore(service.store).get(KIND, str(capsule["run_id"]))
    if existing:
        return existing
    report = weekly_contribution(service.store, week_of=capsule["selection_date"], now=now)
    batch = next((b for b in report["batches"] if b["run_id"] == str(capsule["run_id"])), None)
    return freeze_pair(service.store, batch, capsule)


def evaluate_pair(target, facts, now):
    day = target["entry_at"][:10]
    base = {"entry_date": day, "recorded_at": target["recorded_at"], "initial_cash": target["initial_cash"],
            "packet_id": target["packet_id"], "model": target["model"]}
    if shanghai_instant(target["recorded_at"]) > now:
        return {**base, "status": "pending_close"}
    if now.date().isoformat() < day or now.date().isoformat() == day and now.time() < time(15):
        return {**base, "status": "pending_close"}
    symbols = sorted(set(target["blind"] + target["formal"]))
    missing = [s for s in symbols if s not in facts or not _complete(facts[s], day, now)]
    if missing:
        return {**base, "status": "missing_facts", "missing_symbols": missing}
    if any(any(facts[s]["execution_rules"].get(k) != v for k, v in target["cost_policy"].items()) for s in symbols):
        return {**base, "status": "different_cost_rules"}
    results = {}
    for side in ("blind", "formal"):
        ledger = empty_ledger(target["initial_cash"])
        fills = []
        budget = money(target["initial_cash"]) / len(target[side])
        for symbol in target[side]:
            fact = facts[symbol]
            rules = ExecutionRules(**fact["execution_rules"])
            qty = int(budget / unit_price(fact["open"])) // rules.buy_step * rules.buy_step
            order = {"side": "buy", "quantity": qty, "published_at": target["recorded_at"],
                     "earliest_execution_at": target["entry_at"]}
            if qty < rules.min_buy:
                fills.append({"symbol": symbol, "status": "unfilled", "reason": "below_lot"})
                continue
            fill = simulate_fill(order, fact, rules, cash=min(budget, money(ledger["cash"])), sellable=0)
            if fill.get("reason") in {"invalid_market_data", "execution_data_missing", "raw_or_corporate_action_missing",
                                      "invalid_order", "before_publication", "timezone_missing", "execution_time_missing"}:
                return {**base, "status": "missing_facts", "missing_symbols": [symbol]}
            ledger = apply_fill(ledger, event_id=symbol, symbol=symbol, side="buy", fill=fill)
            fills.append({"symbol": symbol, "status": fill["status"], "reason": fill.get("reason")})
        mark = mark_ledger(ledger, trade_date=day, prices={s: facts[s]["close"] for s in target[side]},
                           corporate_actions_complete=True)["marks"][-1]
        results[side] = {"fees": mark["fees_paid"], "return_pct": mark["net_return_pct"], "fills": fills}
    traded = all(any(f["status"] in {"filled","partially_filled"} for f in res["fills"]) for res in results.values())
    return {**base, **results, "status": "marked" if traded else "no_fills",
            "difference_pp": round(results["blind"]["return_pct"] - results["formal"]["return_pct"], 4) if traded else None,
            "fact_set_hash": payload_hash({s: facts[s]["fact_hash"] for s in symbols}),
            "fact_refs": [{"symbol": s, "id": f"{day}:{s}", "revision": facts[s]["revision"],
                           "hash": facts[s]["fact_hash"]} for s in symbols]}


def _complete(fact, day, now):
    observed = shanghai_instant(fact.get("observed_at"))
    if (not fact.get("fact_hash") or not fact.get("revision") or fact.get("trade_date") != day or fact.get("adjustment") != "raw"
            or not observed or observed > now or observed.date().isoformat() != day or observed.time() < time(15)
            or fact.get("corporate_actions_complete") is not True or fact.get("corporate_action_unresolved")
            or not isinstance(fact.get("execution_rules"), dict)
            or not asdict(ExecutionRules()).keys() <= fact["execution_rules"].keys()):
        return False
    try:
        ExecutionRules(**fact["execution_rules"])
        return unit_price(fact["open"]) > 0 and unit_price(fact["close"]) > 0
    except (ArithmeticError, ValueError, TypeError, KeyError):
        return False
