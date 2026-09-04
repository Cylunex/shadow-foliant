"""Explicit operator entry point for research administration; never places orders."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: E402,F401


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("refresh-quality")
    commands.add_parser("research-weekly")
    compare = commands.add_parser("compare-offline")
    compare.add_argument("--input", required=True, help="JSON exports only; no model binaries or Python")
    holdout = commands.add_parser("seal-holdout")
    holdout.add_argument("--start", required=True)
    holdout.add_argument("--end", required=True)
    rollback = commands.add_parser("rollback-policy")
    rollback.add_argument("--target-hash", required=True)
    rollback.add_argument("--current-hash", required=True)
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--confirm-rollback", action="store_true")
    args = parser.parse_args()
    from application.decision_loop import DecisionLoopService
    service = DecisionLoopService()
    if args.command == "status":
        result = service.dashboard()
    elif args.command == "refresh-quality":
        from jobs.decision_loop_jobs import refresh_quality
        result = refresh_quality(service.store)
    elif args.command == "research-weekly":
        from jobs.decision_loop_jobs import weekly_research_cycle
        result = weekly_research_cycle(service.store)
    elif args.command == "seal-holdout":
        result = service.seal_holdout(start_date=args.start, end_date=args.end)
    elif args.command == "compare-offline":
        source = Path(args.input)
        if not source.is_file() or source.stat().st_size > 8_000_000:
            parser.error("offline export must be a JSON file smaller than 8 MB")
        result = service.compare_offline(json.loads(source.read_text(encoding="utf-8")))
    else:
        if not args.confirm_rollback:
            parser.error("rollback requires --confirm-rollback")
        result = service.rollback_policy(target_hash=args.target_hash, current_hash=args.current_hash, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
