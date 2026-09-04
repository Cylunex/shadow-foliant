"""Optional user-confirmed account facts; no derived cash guesses or trade writes."""
from decimal import Decimal
from analysis.decision_evaluation import money
from application.results import payload_hash
from data.reliability_store import ReliabilityStore


class AccountReconciliation:
    def __init__(self, store):
        self.repo = ReliabilityStore(store)

    def preview(self, rows, *, owner, watermark):
        if not owner or not watermark or not isinstance(rows, list) or not 0 < len(rows) <= 1000:
            raise ValueError("account_import_identity_required")
        allowed = {"cash_balance", "deposit", "withdrawal", "fee", "dividend", "equity_balance"}
        normalized, seen = [], set()
        for row in rows:
            if row.get("currency", "CNY") != "CNY":
                raise ValueError("account_currency_not_supported")
            if row.get("kind") not in allowed or not row.get("date") or not row.get("external_id"):
                raise ValueError("invalid_account_fact")
            from datetime import date
            date.fromisoformat(row["date"])
            if row["external_id"] in seen:
                raise ValueError("duplicate_account_fact")
            seen.add(row["external_id"])
            amount = money(row.get("amount"))
            if amount < 0:
                raise ValueError("use_explicit_direction_not_negative_amount")
            normalized.append({"external_id": row["external_id"], "kind": row["kind"], "date": row["date"],
                               "amount": str(amount), "currency": "CNY"})
        head = self.repo.get("account_watermark", "primary", owner)
        value = {"rows": normalized, "watermark": watermark, "scope": "confirmed_inputs_only", "owner": owner,
                 "account_revision": (head or {}).get("revision", 0)}
        return self.repo.once("account_preview", payload_hash(value), value, owner=owner)

    def confirm(self, preview_id, *, owner, watermark):
        with self.repo.transaction() as cur:
            preview = self.repo.read(cur, "account_preview", preview_id, owner)
            if not preview or preview["watermark"] != watermark:
                raise ValueError("account_preview_stale_or_missing")
            result = self.repo.read(cur, "account_confirmation", preview_id, owner)
            if result:
                return result
            head = self.repo.read(cur, "account_watermark", "primary", owner)
            if (head or {}).get("revision", 0) != preview["account_revision"]:
                raise ValueError("account_watermark_conflict")
            for row in preview["rows"]:
                old = self.repo.read(cur, "account_fact", row["external_id"], owner)
                if old and any(old.get(k) != v for k, v in row.items()):
                    raise ValueError("account_external_id_conflict")
                if not old:
                    self.repo.append(cur, "account_fact", row["external_id"], owner, row)
            result = {"status": "confirmed", "count": len(preview["rows"]), "watermark": payload_hash(preview)}
            self.repo.append(cur, "account_watermark", "primary", owner, {"watermark": result["watermark"]}, head)
            return self.repo.append(cur, "account_confirmation", preview_id, owner, result)

    def reconcile(self, *, owner, opening, closing, securities_pnl, start, end):
        rows = [r for r in self.repo.list("account_fact", owner=owner, limit=2000) if start < r["date"] <= end]
        sums = {k: sum((money(r["amount"]) for r in rows if r["kind"] == k), Decimal(0))
                for k in ("deposit", "withdrawal", "fee", "dividend")}
        explained = money(securities_pnl) + sums["deposit"] - sums["withdrawal"] - sums["fee"] + sums["dividend"]
        difference = money(closing) - money(opening)
        return {"change": str(difference), "explained": str(explained), "unexplained": str(difference - explained),
                "buckets": {k: str(v) for k, v in sums.items()}, "twr": None,
                "status": "reconciled" if difference == explained else "needs_review",
                "scope": "provided_period_balances_and_flows; completeness_not_inferred"}
