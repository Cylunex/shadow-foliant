"""Immutable time and policy boundary for formal research decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import _bootstrap


A_SHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
A_SHARE_OPEN = time(9, 30)
A_SHARE_CLOSE_AVAILABLE = time(15, 30)


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
    """Hash the spec plus the resolved Python/distribution environment, without package data."""
    spec_hash = "unknown"
    for name in ("uv.lock", "requirements.lock", "requirements.txt"):
        path = Path(_bootstrap.ROOT) / name
        try:
            if path.is_file():
                spec_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                break
        except OSError:
            continue
    installed = sorted({
        f"{str(dist.metadata.get('Name') or '').lower()}=={dist.version}"
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    })
    payload = {
        "spec_hash": spec_hash,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "distributions": installed,
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


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
            now = datetime.now(A_SHARE_TIMEZONE)
            default_time = time(9, 0) if mode == "preopen" else A_SHARE_CLOSE_AVAILABLE
            if (
                selection == now.date().isoformat()
                and ((mode == "preopen" and now.time() < A_SHARE_OPEN)
                     or (mode != "preopen" and now.time() >= A_SHARE_CLOSE_AVAILABLE))
            ):
                decision = now
            else:
                decision = datetime.combine(
                    pd.Timestamp(selection).date(), default_time, tzinfo=A_SHARE_TIMEZONE
                )
        else:
            decision = pd.Timestamp(decision_at).to_pydatetime()
            if decision.tzinfo is None:
                decision = decision.replace(tzinfo=A_SHARE_TIMEZONE)
            else:
                decision = decision.astimezone(A_SHARE_TIMEZONE)
        if decision.date().isoformat() != selection:
            raise ValueError("decision_at must be on selection_date in Asia/Shanghai")
        local_time = decision.timetz().replace(tzinfo=None)
        if mode == "preopen" and local_time >= A_SHARE_OPEN:
            raise ValueError("preopen decision_at must be before the A-share open")
        if mode in {"postclose", "historical_close"} and local_time < A_SHARE_CLOSE_AVAILABLE:
            raise ValueError("close decision_at must be at or after 15:30 Asia/Shanghai")
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
        cutoff_at = decision.astimezone(A_SHARE_TIMEZONE).isoformat()
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
