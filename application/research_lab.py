"""Optional hardened offline worker. Production never executes generated Python."""
from __future__ import annotations
import json
import os
import re
import subprocess
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
