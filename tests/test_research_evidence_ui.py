"""Exercise the browser editor state with Node; HTTP/domain checks live in test_research_claims."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_research_evidence_editor_workflow():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser editor workflow")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("research_evidence_ui.mjs"))],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
