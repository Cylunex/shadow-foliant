#!/usr/bin/env python3
"""Atomically install the private scheduled launcher as root-only executable.

The launcher source lives outside this repository. This installer verifies its
identity, keeps the previous inode as a rollback copy, and sets mode 0700 on
the replacement *before* the atomic rename. It never runs the candidate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
from uuid import uuid4


DEFAULT_DESTINATION = Path(
    "/data/project/shadow-foliant-ops/runtime/foliant-scheduled-heartbeat.py"
)
MAX_SOURCE_BYTES = 1024 * 1024
PRIVATE_EXECUTABLE_MODE = 0o700
EXPECTED_SHEBANG = b"#!/data/project/shadow-foliant-ops/current/venv2/bin/python\n"
VERSION_PATTERN = re.compile(r"foliant-scheduled-heartbeat-v[0-9]+")


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_SOURCE_BYTES:
            raise ValueError("launcher_candidate_invalid")
        content = os.read(descriptor, MAX_SOURCE_BYTES + 1)
        if not content or len(content) > MAX_SOURCE_BYTES:
            raise ValueError("launcher_candidate_invalid")
        return content
    finally:
        os.close(descriptor)


def _version(content: bytes) -> str:
    if not content.startswith(EXPECTED_SHEBANG):
        raise ValueError("launcher_shebang_invalid")
    source = content.decode("utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "WRAPPER_VERSION"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            version = node.value.value
            if VERSION_PATTERN.fullmatch(version):
                return version
    raise ValueError("launcher_version_invalid")


def install(candidate: Path, destination: Path, *, expected_sha256: str,
            expected_version: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("launcher_checksum_invalid")
    if not VERSION_PATTERN.fullmatch(expected_version):
        raise ValueError("launcher_version_invalid")
    destination = Path(destination)
    candidate = Path(candidate)
    content = _read_regular(candidate)
    digest = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise ValueError("launcher_checksum_mismatch")
    version = _version(content)
    if version != expected_version:
        raise ValueError("launcher_version_mismatch")
    current = os.lstat(destination)
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("launcher_destination_invalid")
    directory = destination.parent
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        raise ValueError("launcher_directory_invalid")

    suffix = uuid4().hex[:12]
    staged = directory / ("." + destination.name + ".new-" + suffix)
    backup = directory / (destination.name + ".pre-" + version + "-" + suffix)
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), PRIVATE_EXECUTABLE_MODE)
            if current.st_uid != os.geteuid() or current.st_gid != os.getegid():
                os.fchown(handle.fileno(), current.st_uid, current.st_gid)
            os.fsync(handle.fileno())
        os.link(destination, backup, follow_symlinks=False)
        os.replace(staged, destination)
        directory_descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        staged.unlink(missing_ok=True)
    installed = os.lstat(destination)
    if (not stat.S_ISREG(installed.st_mode)
            or stat.S_IMODE(installed.st_mode) != PRIVATE_EXECUTABLE_MODE):
        raise RuntimeError("launcher_install_mode_invalid")
    return {"version": version, "sha256": digest, "mode": "0700",
            "backup": str(backup)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the protected scheduled launcher")
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    args = parser.parse_args()
    result = install(args.candidate, args.destination,
                     expected_sha256=args.expected_sha256,
                     expected_version=args.expected_version)
    print("launcher_installed version={version} sha256={sha256} mode={mode}".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
