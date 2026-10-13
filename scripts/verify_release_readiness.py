"""Verify protected readiness JSON without echoing its private response body."""
from __future__ import annotations

import argparse
import json
import sys


def verify(payload: object, *, expected_revision: str | None = None) -> bool:
    if not isinstance(payload, dict) or type(payload.get("ready")) is not bool:
        return False
    checks = payload.get("checks")
    if not isinstance(checks, (dict, list)) or not checks:
        return False
    if expected_revision is None:
        # A data bootstrap may legitimately be incomplete, but not malformed.
        return True
    if payload.get("ready") is not True or payload.get("revision") != expected_revision:
        return False
    return isinstance(checks, dict) and all(
        isinstance(checks.get(key), dict) and checks[key].get("ok") is True
        for key in ("database", "manual_queue", "revision")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    try:
        valid = verify(json.load(sys.stdin), expected_revision=args.expected_revision)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        print("release_readiness_contract_invalid", file=sys.stderr)
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
