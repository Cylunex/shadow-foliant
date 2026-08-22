#!/usr/bin/env python3
"""Operate the local research warehouse without printing market payloads."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Foliant local research data pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap", help="backfill 400-500 trading days")
    bootstrap.add_argument("--end-date")
    bootstrap.add_argument("--trading-days", type=int, default=500, choices=range(320, 501))
    sync = sub.add_parser("sync", help="sync one market day plus PIT fundamentals")
    sync.add_argument("--date", default=date.today().isoformat())
    select = sub.add_parser("select", help="run the local primary selector")
    select.add_argument("--date", default=date.today().isoformat())
    select.add_argument(
        "--data-cutoff",
        help="explicit completed market date to include (for post-close/manual replay)",
    )
    sub.add_parser("contracts", help="show credential-free source capability limits")
    args = parser.parse_args()

    if args.command == "contracts":
        from data.source_contracts import contracts
        result = contracts()
    elif args.command == "bootstrap":
        from data.research_sync import ResearchSynchronizer
        result = ResearchSynchronizer().bootstrap(
            end_date=args.end_date, trading_days=args.trading_days
        )
    elif args.command == "sync":
        from data.research_sync import ResearchSynchronizer
        syncer = ResearchSynchronizer()
        result = {"master": syncer.sync_master(), "day": syncer.sync_day(args.date)}
    else:
        from analysis.local_stock_selector import LocalStockSelector
        selected = LocalStockSelector().run(
            args.date, data_cutoff=args.data_cutoff, persist=True
        )
        result = {
            "status": selected.get("status"), "selection_date": selected.get("selection_date"),
            "coverage": selected.get("coverage"), "candidate_count": len(selected.get("candidates", [])),
            "run_id": selected.get("run_id"), "metadata": selected.get("metadata", {}),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status", "success") not in {"error", "incomplete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
