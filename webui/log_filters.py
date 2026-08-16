"""Logging filters that keep callback credentials and query data out of access logs."""

from __future__ import annotations

import logging


class RedactRequestQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5 and isinstance(args[2], str):
            target = args[2]
            if "?" in target:
                target = target.split("?", 1)[0] + "?<redacted>"
                record.args = (args[0], args[1], target, *args[3:])
        return True
