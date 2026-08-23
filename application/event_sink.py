"""Optional HTTP adapter for metadata-only domain events."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


def configured_http_publisher():
    endpoint = os.getenv("FOLIANT_OUTBOX_ENDPOINT", "").strip()
    bearer_file = os.getenv("FOLIANT_OUTBOX_BEARER_FILE", "").strip()
    if not endpoint or not bearer_file:
        return None
    token_path = Path(bearer_file)
    if not token_path.is_file():
        raise RuntimeError("configured outbox Bearer file is unavailable")

    def publish(event_type: str, payload: dict) -> None:
        token = token_path.read_text(encoding="utf-8").strip()
        body = json.dumps(
            {"event_type": event_type, "payload": payload},
            ensure_ascii=True, separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            endpoint, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urlopen(request, timeout=10) as response:  # noqa: S310 - endpoint is operator config
            if int(response.status) < 200 or int(response.status) >= 300:
                raise RuntimeError("event sink rejected the event")

    return publish
