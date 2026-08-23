"""Immutable time and policy boundary for formal research decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

import _bootstrap


def _iso_date(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()


def code_revision() -> str:
    configured = os.getenv("APP_REVISION", "").strip()
    if configured:
        return configured
    try:
        release_revision = (Path(_bootstrap.ROOT) / ".release-revision").read_text(
            encoding="utf-8"
        ).strip()
        if release_revision:
            return release_revision
    except OSError:
        pass
    git_dir = Path(_bootstrap.ROOT) / ".git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:].strip()
        ref_path = git_dir.joinpath(*ref.split("/"))
        if ref_path.is_file():
            return ref_path.read_text(encoding="utf-8").strip()
        for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
            revision, _, name = line.partition(" ")
            if name == ref:
                return revision
    except OSError:
        pass
    return "unknown"


def dependency_lock_hash() -> str:
    """Hash the checked-in dependency specification without exposing its contents."""
    for name in ("uv.lock", "requirements.lock", "requirements.txt"):
        path = Path(_bootstrap.ROOT) / name
        try:
            if path.is_file():
                return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return "unknown"


@dataclass(frozen=True)
class DecisionContext:
    selection_date: str
    decision_at: str
    market_cutoff: str
    market_cutoff_inclusive: bool
    universe_cutoff: str
    financial_cutoff_at: str
    event_cutoff_at: str
    mode: str
    policy_version: str
    policy_hash: str
    code_revision: str

    @classmethod
    def build(
        cls,
        selection_date: Optional[str] = None,
        *,
        data_cutoff: Optional[str] = None,
        decision_at: Optional[datetime | str] = None,
        mode: str = "preopen",
        policy_version: str,
        policy_hash: str,
    ) -> "DecisionContext":
        selection = _iso_date(selection_date or date.today())
        mode = str(mode or "preopen").strip().lower()
        if mode not in {"preopen", "postclose", "historical_close"}:
            raise ValueError("decision mode must be preopen, postclose, or historical_close")
        if decision_at is None:
            decision = datetime.now().astimezone()
            if selection != decision.date().isoformat():
                decision = datetime.combine(
                    pd.Timestamp(selection).date(), time(9, 30), tzinfo=decision.tzinfo
                )
        else:
            decision = pd.Timestamp(decision_at).to_pydatetime()
            if decision.tzinfo is None:
                decision = decision.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if data_cutoff:
            market_cutoff = _iso_date(data_cutoff)
            if market_cutoff >= selection and mode == "preopen":
                raise ValueError("preopen decisions cannot include selection-date market data")
            inclusive = True
        elif mode == "preopen":
            market_cutoff = (pd.Timestamp(selection).date() - timedelta(days=1)).isoformat()
            inclusive = True
        else:
            market_cutoff = selection
            inclusive = True
        if market_cutoff > selection:
            raise ValueError("market cutoff cannot be after selection date")
        cutoff_at = decision.astimezone().isoformat()
        return cls(
            selection_date=selection,
            decision_at=cutoff_at,
            market_cutoff=market_cutoff,
            market_cutoff_inclusive=inclusive,
            universe_cutoff=selection,
            financial_cutoff_at=cutoff_at,
            event_cutoff_at=cutoff_at,
            mode=mode,
            policy_version=str(policy_version),
            policy_hash=str(policy_hash),
            code_revision=code_revision(),
        )

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def manifest_seed(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
