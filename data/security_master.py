"""Current-listed membership and observed industry evidence for immutable masters."""
from __future__ import annotations

from datetime import datetime
import pandas as pd


def prepare_master(frame: pd.DataFrame) -> pd.DataFrame:
    """Do not mistake a current-only response for a complete lifecycle dataset."""
    from application.industry_classification import classify_symbols

    if frame is None or frame.empty:
        return pd.DataFrame()
    out = frame.copy()
    out.attrs = dict(frame.attrs)
    scope = "lifecycle" if frame.attrs.get("lifecycle_complete") else "current_listed"
    if scope == "current_listed":
        if "L" not in frame.attrs.get("available_list_statuses", []):
            raise ValueError("current_master_listed_status_unverified")
        out = out[out["list_status"].isin(["L", "P"])].copy()
    out.attrs["universe_scope"] = scope
    observed = datetime.now().astimezone().isoformat()
    symbols = out["ts_code"].astype(str).str.split(".").str[0]
    classification = classify_symbols(symbols.tolist(), as_of=observed)
    if classification.get("source") and classification.get("rows"):
        by_symbol = {row["symbol"]: row for row in classification["rows"]}
        # Use one taxonomy for the entire snapshot. Never mix provider labels
        # with Shenwan L1 or fill conflicts by guessing from company names.
        out["industry"] = symbols.map(
            lambda symbol: by_symbol.get(symbol, {}).get("industry_l1_name") or ""
        )
        out["industry_evidence"] = symbols.map(lambda symbol: {
            **by_symbol.get(symbol, {}),
            "provider": "swsresearch", "taxonomy": "sw2021_l1",
            "source_sha256": classification["source"]["csv_sha256"],
            "observed_at": classification["source"]["observed_at"],
        })
    out.attrs["industry_classification"] = {
        key: classification.get(key) for key in
        ("status", "coverage", "source", "reason", "conflict_symbols", "unknown_symbols")
    }
    out.attrs["provenance"] = {
        **out.attrs.get("provenance", {}), "retrieved_at": observed,
    }
    return out
