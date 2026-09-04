"""Operator-only real evaluation: reserve sealed batch, isolated arithmetic, retire.

Requires an already registered/evaluated trial and private digest-pinned facts.
Never run from an Agent-controlled shell or with production account material.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _bootstrap
from application.decision_loop import DecisionLoopService
from application.research_lab import isolated_holdout_evaluator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    parser.add_argument("--trial", required=True)
    parser.add_argument("--facts", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--confirm-consume", action="store_true")
    args = parser.parse_args()
    if not args.confirm_consume:
        parser.error("--confirm-consume is required; reservation is irreversible")
    result = DecisionLoopService().consume_holdout(args.batch, args.trial,
        evaluator=isolated_holdout_evaluator(args.facts, image=args.image, expected_digest=args.sha256))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
