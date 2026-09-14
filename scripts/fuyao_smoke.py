#!/usr/bin/env python3
"""Read-only, secret-safe smoke checks for the Fuyao official provider."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import _bootstrap  # noqa: F401
from data.sources import fuyao_aicubes as source


def check(name, call):
    started = time.monotonic()
    try:
        value = call()
        if hasattr(value, "empty"):
            rows = len(value)
            ok = not value.empty
        elif isinstance(value, dict):
            rows = len(value.get("items", value))
            ok = bool(value) and value.get("status") not in {"degraded", "invalid_request"}
        else:
            rows = len(value or [])
            ok = bool(value)
        return {"status": "ok" if ok else "degraded", "rows": rows,
                "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__,
                "latency_ms": round((time.monotonic() - started) * 1000)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--secret-key", default="fuyao-aicubes")
    args = parser.parse_args()
    os.environ["FUYAO_AICUBES_API_KEY_FILE"] = os.path.abspath(args.secret_file)
    os.environ["FUYAO_AICUBES_SECRET_KEY"] = args.secret_key
    os.environ["FUYAO_AICUBES_ENABLED"] = "true"
    if not source.available():
        print(json.dumps({"provider": source.PROVIDER, "configured": False,
                          "status": "not_configured"}, ensure_ascii=False))
        return 2
    today = date.today()
    start = (today - timedelta(days=14)).isoformat()
    results = {
        "calendar": check("calendar", lambda: source.get_trade_calendar_evidence(start, today.isoformat(), use_cache=False)),
        "snapshot": check("snapshot", lambda: source.get_quotes(["000001", "600000"], use_cache=False)),
        "historical": check("historical", lambda: source.get_kline("000001", period="1mo", use_cache=False)),
        "valuation": check("valuation", lambda: source.get_valuations(["000001", "600000"], use_cache=False)),
        "financials": check("financials", lambda: source.get_financials("000001", limit=1, use_cache=False)),
        "financial_indicators": check(
            "financial_indicators",
            lambda: source.get_financial_indicators(
                "000001", f"{today.year}-{max(1, min(4, (today.month - 1) // 3))}",
                use_cache=False,
            ),
        ),
        "auction": check("auction", lambda: source.get_auction_snapshot(
            ["000001", "600000"], stage="final", use_cache=False
        )),
        "special_data": check("special_data", lambda: source.get_special_data(
            "limit_up_pool", size=1
        )),
        "capital_flow": {"status": "degraded", "reason": "external_access_unavailable"},
    }
    print(json.dumps({"provider": source.PROVIDER, "configured": True,
                      "results": results}, ensure_ascii=False, sort_keys=True))
    return 0 if all(results[name]["status"] == "ok"
                    for name in ("calendar", "snapshot", "historical")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
