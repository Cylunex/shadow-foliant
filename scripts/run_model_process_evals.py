"""Explicit real-model evaluation; default only lists synthetic development cases."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _bootstrap
from agent.process_evals import development_cases, run_case


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--configuration", choices=["baseline", "curated", "self_generated", "all"], default="baseline")
    parser.add_argument("--case-limit", type=int, default=24)
    parser.add_argument("--sealed-file", type=Path)
    args = parser.parse_args()
    if args.sealed_file and not args.execute:
        raise ValueError("sealed_cases_not_available_for_discovery")
    cases = development_cases()
    if args.sealed_file:
        if args.sealed_file.stat().st_mode & 0o077 or args.sealed_file.stat().st_size > 1048576:
            raise ValueError("sealed_case_file_must_be_private_and_bounded")
        cases = json.loads(args.sealed_file.read_text())
    if not args.execute:
        print(json.dumps({"status": "not_executed", "case_count": len(cases), "ids": [c["id"] for c in cases]}))
        return 0
    from core.llm_router import get_router
    call = get_router().call
    configs = ["baseline", "curated", "self_generated"] if args.configuration == "all" else [args.configuration]
    results = []
    for config in configs:
        for case in cases[:max(1, min(24, args.case_limit))]:
            for repeat in range(3 if case.get("critical") else 1):
                result = run_case(case, call=call, configuration=config)
                results.append({**result, "repeat": repeat + 1})
                print(json.dumps(results[-1], ensure_ascii=False), flush=True)
                if result["status"] == "model_unavailable":
                    return 2
    return 0 if all(r.get("passed") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
