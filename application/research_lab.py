"""Optional hardened offline worker. Production never executes generated Python."""
from __future__ import annotations
import json
import os
import re
import subprocess
import shutil
from analysis.factor_ast import validate_ast


def run_isolated(ast, fields, *, image, timeout=20):
    validate_ast(ast, allowed_fields=set(fields))
    if not re.fullmatch(r"[a-zA-Z0-9._/:\-]+@sha256:[a-f0-9]{64}", image or ""):
        raise ValueError("pinned_research_image_required")
    payload = json.dumps({"ast": ast, "fields": fields}, allow_nan=False)
    if len(payload.encode()) > 2_000_000:
        raise ValueError("research_input_budget")
    # No mounts, no credentials, no holdout files, no inherited Docker context.
    # Container lifetime is bounded independently of the CLI timeout.
    command = ["docker", "run", "--rm", "--pull=never", "-i", "--network=none", "--read-only",
               "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
               "--memory=256m", "--memory-swap=256m", "--cpus=1", "--pids-limit=32",
               "--ulimit=cpu=20:20", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", image]
    result = subprocess.run(command, input=payload, text=True, capture_output=True,
                            timeout=max(1, min(30, timeout)),
                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, check=False)
    if result.returncode or len(result.stdout) > 4_000_000:
        raise RuntimeError("isolated_research_worker_failed")
    return json.loads(result.stdout)


def isolated_holdout_evaluator(path, *, image, expected_digest):
    """Operator-only factory: file is read only AFTER atomic batch reservation.

    Sealed facts live outside the training workspace; worker receives only fixed
    rows, has no mounts/network/secrets. This cannot hide public market knowledge.
    """
    from pathlib import Path
    import hashlib
    import stat

    def validate_source():
        source = Path(path)
        facts = source.lstat()
        if not stat.S_ISREG(facts.st_mode) or facts.st_mode & 0o077 or facts.st_size > 2_000_000:
            raise ValueError('sealed_facts_require_private_bounded_file')
        return source

    # No sealed contents are read before reservation. Verify infrastructure with
    # public synthetic values, so missing Docker/image/worker doesn't burn a batch.
    validate_source()
    if not re.fullmatch(r'[a-f0-9]{64}', expected_digest or ''):
        raise ValueError('sealed_facts_digest_required')
    if not shutil.which('docker'):
        raise RuntimeError('isolated_research_docker_unavailable_before_reservation')
    probe = run_isolated({'op': 'field', 'name': 'probe'}, {'probe': [1.0]}, image=image)
    if not isinstance(probe, dict) or probe.get('values') != [1.0]:
        raise RuntimeError('isolated_research_preflight_failed')

    def evaluate(batch, trial):
        source = validate_source()
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError("sealed_facts_digest_mismatch")
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("invalid_sealed_rows")
        # The isolated DSL performs the arithmetic; no candidate-generated Python.
        fields = {k: [r[k] for r in rows] for k in ("gross_return_pct", "cost_pct", "baseline_return_pct")}
        ast = {"op": "sub", "args": [{"op": "sub", "args": [
            {"op": "field", "name": "gross_return_pct"}, {"op": "field", "name": "cost_pct"}]},
            {"op": "field", "name": "baseline_return_pct"}]}
        result = run_isolated(ast, fields, image=image)
        if len(result["values"]) != len(rows):
            raise ValueError("isolated_evaluation_length_mismatch")
        for row, value in zip(rows, result["values"]):
            if abs(value - (row["gross_return_pct"] - row["cost_pct"] - row["baseline_return_pct"])) > 1e-8:
                raise ValueError("isolated_evaluation_number_mismatch")
        return rows
    return evaluate
