"""Point-in-time, operator-curated concept memberships for current stocks.

The JSON lives outside the tracked source tree in production. A membership is
only evidence that a stock was in a selected concept at ``observed_at``; it is
not a historical membership series or an authority for trading decisions.
"""

from __future__ import annotations

from datetime import date, datetime, time
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


_TZ = ZoneInfo("Asia/Shanghai")
_SYMBOL = re.compile(r"^[0-9]{6}$")
_THSCODE = re.compile(r"^(885|886)[0-9]{3}\.TI$")
_MAX_BYTES = 64 * 1024


def _decision_time(value: str) -> datetime:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.max, _TZ)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=_TZ) if parsed.tzinfo is None else parsed.astimezone(_TZ)


@lru_cache(maxsize=2)
def _read(path: str, size: int, mtime_ns: int) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    if len(raw) != size:
        raise ValueError("manual_concepts_source_changed")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manual_concepts_source_unreadable") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("manual_concepts_schema_invalid")
    observed = datetime.fromisoformat(str(data.get("observed_at") or ""))
    if observed.tzinfo is None:
        raise ValueError("manual_concepts_observed_at_unzoned")
    concepts = data.get("concepts")
    if not isinstance(concepts, list) or not concepts or len(concepts) > 100:
        raise ValueError("manual_concepts_list_invalid")
    seen_labels: set[str] = set()
    seen_codes: set[str] = set()
    for concept in concepts:
        if not isinstance(concept, dict):
            raise ValueError("manual_concepts_item_invalid")
        label = concept.get("label")
        code = concept.get("thscode")
        symbols = concept.get("symbols")
        if (not isinstance(label, str) or not 1 <= len(label) <= 32
                or not isinstance(code, str) or not _THSCODE.fullmatch(code)
                or not isinstance(symbols, list) or not symbols
                or len(symbols) > 100 or any(not isinstance(s, str) or
                                             not _SYMBOL.fullmatch(s) for s in symbols)
                or len(symbols) != len(set(symbols))
                or label in seen_labels or code in seen_codes):
            raise ValueError("manual_concepts_item_invalid")
        seen_labels.add(label)
        seen_codes.add(code)
    return data, hashlib.sha256(raw).hexdigest()


def classify_manual_concepts(stocks: list[str], *, as_of: str) -> dict[str, Any]:
    """Return selected current concept tags, never backfilling pre-observation dates."""
    empty: dict[str, Any] = {
        "theme_status": "missing", "theme_scope": "selected_current_concepts",
        "theme_groups": [], "theme_by_symbol": {}, "tagged_stock_count": 0,
    }
    configured = os.getenv("FOLIANT_MANUAL_CONCEPTS_JSON", "").strip()
    if not configured:
        return {**empty, "theme_reason": "manual_concepts_source_unconfigured"}
    path = Path(configured)
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("manual_concepts_source_invalid")
        stat = path.stat()
        if not 0 < stat.st_size <= _MAX_BYTES:
            raise ValueError("manual_concepts_source_size_invalid")
        data, digest = _read(str(path), stat.st_size, stat.st_mtime_ns)
        observed = datetime.fromisoformat(data["observed_at"]).astimezone(_TZ)
        if _decision_time(as_of) < observed:
            raise ValueError("manual_concepts_observed_after_decision")
    except (OSError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "manual_concepts_source_unreadable"
        return {**empty, "theme_reason": reason}
    selected = set(stocks)
    groups = []
    by_symbol: dict[str, list[dict[str, str]]] = {}
    for concept in data["concepts"]:
        hits = sorted(selected.intersection(concept["symbols"]))
        if not hits:
            continue
        groups.append({
            "label": concept["label"], "thscode": concept["thscode"],
            "holding_count": len(hits), "symbols": hits,
        })
        for symbol in hits:
            by_symbol.setdefault(symbol, []).append({
                "label": concept["label"], "thscode": concept["thscode"],
            })
    groups.sort(key=lambda row: (-row["holding_count"], row["label"]))
    return {
        "theme_status": "partial" if by_symbol else "missing",
        "theme_scope": "selected_current_concepts",
        "theme_groups": groups, "theme_by_symbol": by_symbol,
        "tagged_stock_count": len(by_symbol),
        "theme_source": {
            "provider": "fuyao_aicubes_ths_constituents",
            "curation": "manual_snapshot", "observed_at": observed.isoformat(),
            "json_sha256": digest, "concept_count": len(data["concepts"]),
        },
    }
