#!/usr/bin/env python3
"""One-time private Fernet key provisioning for message archive releases."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from cryptography.fernet import Fernet


DEFAULT_PATH = Path("/data/project/shadow-foliant-ops/secrets/message-archive.key")


def provision(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("message_archive_key_permissions_invalid")
            Fernet(os.read(descriptor, 256).strip())
        finally:
            os.close(descriptor)
        return "existing"
    try:
        os.write(descriptor, Fernet.generate_key() + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "created"


if __name__ == "__main__":
    configured = os.getenv("FOLIANT_MESSAGE_ARCHIVE_KEY_FILE")
    print("message_archive_key=" + provision(Path(configured) if configured else DEFAULT_PATH))
