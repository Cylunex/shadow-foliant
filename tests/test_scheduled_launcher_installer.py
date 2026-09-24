from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

import pytest

from scripts.install_scheduled_heartbeat import install


SHEBANG = b"#!/data/project/shadow-foliant-ops/current/venv2/bin/python\n"
OLD = SHEBANG + b'WRAPPER_VERSION = "foliant-scheduled-heartbeat-v6"\n'
NEW = SHEBANG + b'WRAPPER_VERSION = "foliant-scheduled-heartbeat-v7"\n'


def _files(tmp_path: Path):
    destination = tmp_path / "foliant-scheduled-heartbeat.py"
    destination.write_bytes(OLD)
    destination.chmod(0o700)
    candidate = tmp_path / "candidate.py"
    candidate.write_bytes(NEW)
    candidate.chmod(0o644)
    return candidate, destination


def _install(candidate: Path, destination: Path):
    return install(candidate, destination, expected_sha256=hashlib.sha256(NEW).hexdigest(),
                   expected_version="foliant-scheduled-heartbeat-v7")


def test_atomic_install_restores_root_only_executable_mode_and_keeps_backup(tmp_path):
    candidate, destination = _files(tmp_path)
    result = _install(candidate, destination)
    assert destination.read_bytes() == NEW
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert os.access(destination, os.X_OK)
    backup = Path(result["backup"])
    assert backup.read_bytes() == OLD
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert not list(tmp_path.glob(".foliant-scheduled-heartbeat.py.new-*"))


def test_bad_candidate_or_failed_replace_cannot_change_live_launcher(tmp_path, monkeypatch):
    candidate, destination = _files(tmp_path)
    with pytest.raises(ValueError, match="checksum_mismatch"):
        install(candidate, destination, expected_sha256="0" * 64,
                expected_version="foliant-scheduled-heartbeat-v7")
    assert destination.read_bytes() == OLD

    from scripts import install_scheduled_heartbeat as installer
    monkeypatch.setattr(installer.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        _install(candidate, destination)
    assert destination.read_bytes() == OLD
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert not list(tmp_path.glob(".foliant-scheduled-heartbeat.py.new-*"))


def test_symlink_candidate_and_destination_are_rejected(tmp_path):
    candidate, destination = _files(tmp_path)
    candidate_link = tmp_path / "candidate-link.py"
    candidate_link.symlink_to(candidate)
    with pytest.raises(OSError):
        _install(candidate_link, destination)

    destination.unlink()
    destination.symlink_to(candidate)
    with pytest.raises(ValueError, match="destination_invalid"):
        _install(candidate, destination)


def test_wrong_runtime_shebang_is_rejected_before_replacement(tmp_path):
    candidate, destination = _files(tmp_path)
    altered = NEW.replace(SHEBANG, b"#!/usr/bin/env python3\n")
    candidate.write_bytes(altered)
    with pytest.raises(ValueError, match="shebang_invalid"):
        install(candidate, destination,
                expected_sha256=hashlib.sha256(altered).hexdigest(),
                expected_version="foliant-scheduled-heartbeat-v7")
    assert destination.read_bytes() == OLD
