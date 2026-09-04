"""Audit by default; explicitly capture bounded legacy history for future runs."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: E402,F401
from data.market_history_freeze import audit_history, freeze_batch
from data.research_store import ResearchStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--through', required=True, help='Inclusive YYYY-MM-DD')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--max-batches', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 10000 or not 1 <= args.max_batches <= 20:
        parser.error('batch-size must be 1..10000; max-batches must be 1..20')
    store = ResearchStore(ensure_schema=False)
    print(json.dumps({'before': audit_history(store, through=args.through)}, default=str), flush=True)
    if args.apply:
        for _ in range(args.max_batches):
            result = freeze_batch(store, through=args.through, limit=args.batch_size)
            print(json.dumps(result), flush=True)
            if result['frozen_rows'] < args.batch_size:
                break
        print(json.dumps({'after': audit_history(store, through=args.through)}, default=str), flush=True)


if __name__ == '__main__':
    main()
