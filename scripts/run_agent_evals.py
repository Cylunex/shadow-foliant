"""Execute repository-owned Agent contract fixtures, without spending LLM tokens.

These checks enforce application contracts, not a guarantee about arbitrary model
wording. The fixed YAML cannot inject shell flags, imports or a different test root.
"""
from pathlib import Path
import re
import subprocess
import sys
import yaml


def main():
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "agent/evals/plugin-contract-cases.yaml").read_text())
    nodes = []
    for case in manifest["cases"]:
        node = case.get("pytest_node", "")
        if not re.fullmatch(r"tests/test_[a-z0-9_]+\.py::test_[a-z0-9_]+", node):
            raise ValueError("invalid_agent_eval_target")
        nodes.append(node)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *nodes], cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
