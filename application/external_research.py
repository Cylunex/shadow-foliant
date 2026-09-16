"""Bounded external research overlay for the independent selection lane.

The service accepts explicit structured evidence; it never browses, reads session
history, changes formal/Wencai membership, or creates executable prices.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import math
import re
from typing import Any, Callable

from application.results import clean_json, now_iso, payload_hash, provenance, tool_result
from analysis.independent_selector import artifact_payload as independent_artifact_payload


CHANNEL = "codex-external-independent-v1"
SOURCE_TYPES = {"announcement", "policy", "macro", "industry", "news"}
CONTROVERSY_STATUSES = {"confirmed", "unresolved", "disputed"}
MARKET_REGIMES = {"bull", "sideways", "bear", "risk_off", "unknown"}
HORIZONS = (1, 3, 5, 10, 20)
MIN_EVENT_ADJUSTMENT = -15.0
MAX_EVENT_ADJUSTMENT = 8.0
MIN_TUNING_SAMPLES = 20
MIN_TUNING_WEEKS = 4
CONTEMPORANEOUS_GRACE = timedelta(minutes=15)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
NOTIFICATION_SLOT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:10:15|11:25|14:35|20:45)\+08:00$"
)


def _encode(value: Any) -> str:
    return json.dumps(clean_json(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _decode(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _dt(value: Any, field: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed


def _symbol(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else ""


def _finite(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}_invalid")
    return number


def _exact(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError(f"{label}_unexpected_fields")


class ExternalIndependentResearchService:
    """Persist and project an external ranking without crossing selection boundaries."""

    def __init__(self, store: Any = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if store is None:
            from data.research_store import ResearchStore

            store = ResearchStore(ensure_schema=False)
        self.store = store
        self.clock = clock or (lambda: datetime.now().astimezone())

    def _formal_base(self, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        formal = self.store.formal_selection(run_id) if run_id else None
        if not formal:
            raise ValueError("formal_selection_run_missing")
        independent = independent_artifact_payload(formal.get("artifacts") or {})
        if (independent.get("status") != "ready"
                or independent.get("strategy_version") != "codex-independent-v1"):
            raise ValueError("independent_base_not_ready")
        rows = independent.get("top15") or []
        if len(rows) != 15:
            raise ValueError("independent_base_top15_incomplete")
        return formal, independent

    @staticmethod
    def _idempotency_key(value: Any) -> str:
        key = str(value or "").strip()
        if not IDEMPOTENCY_PATTERN.fullmatch(key):
            raise ValueError("external_idempotency_key_invalid")
        return key

    def _submission_replay(
        self, *, idempotency_key: str, request_hash: str,
    ) -> dict[str, Any] | None:
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT s.request_hash,s.notification_status,o.payload
                   FROM external_research_submissions s
                   JOIN external_independent_overlays o ON o.overlay_id=s.overlay_id
                   WHERE s.idempotency_key=? AND s.channel=?""",
                (idempotency_key, CHANNEL),
            )
            row = cur.fetchone()
            if not row:
                return None
            if str(row[0]) != request_hash:
                raise ValueError("external_idempotency_key_conflict")
            overlay = _decode(row[2])
            data = {
                "status": "ready", "channel": CHANNEL, "overlay": overlay,
                "idempotency": {
                    "key": idempotency_key, "replayed": True,
                    "notification_status": str(row[1]),
                },
                "auto_apply": False, "auto_execution": False,
            }
            return tool_result(
                summary="External independent research submission already exists.",
                resource_uri=(
                    f"shadow://foliant/external-independent/{overlay.get('overlay_id')}"
                ),
                status="complete",
                provenance_value=provenance(
                    run_id=overlay.get("overlay_id") or "external-independent-replay",
                    decision_at=overlay.get("decision_as_of"),
                    market_as_of=overlay.get("base_market_as_of"),
                ),
                data=data, model_payload=data,
            )
        finally:
            conn.close()

    def _evidence(self, raw: dict[str, Any], *, decision: datetime) -> dict[str, Any]:
        allowed = {
            "source_url", "source_type", "published_at", "event_at", "captured_at",
            "symbols", "industries", "direction", "confidence", "expiry",
            "dedupe_key", "primary_source_confirmed", "controversy_status",
        }
        _exact(raw, allowed, "external_evidence")
        source_url = str(raw.get("source_url") or "").strip()
        if not source_url.startswith(("https://", "http://")):
            raise ValueError("source_url_invalid")
        source_type = str(raw.get("source_type") or "")
        if source_type not in SOURCE_TYPES:
            raise ValueError("source_type_invalid")
        published = _dt(raw.get("published_at"), "published_at")
        captured = _dt(raw.get("captured_at"), "captured_at")
        event = _dt(raw.get("event_at"), "event_at") if raw.get("event_at") else None
        expiry = _dt(raw.get("expiry"), "expiry")
        if published > captured or captured > decision:
            raise ValueError("post_decision_evidence_forbidden")
        if expiry < decision:
            raise ValueError("expired_evidence_forbidden")
        symbols = sorted(filter(None, {_symbol(item) for item in raw.get("symbols") or []}))
        industries = sorted({str(item).strip() for item in raw.get("industries") or []
                             if str(item).strip()})
        direction = _finite(raw.get("direction"), "direction")
        confidence = _finite(raw.get("confidence"), "confidence")
        if not -1 <= direction <= 1 or not 0 <= confidence <= 1:
            raise ValueError("evidence_bounds_invalid")
        dedupe_key = str(raw.get("dedupe_key") or "").strip()
        if not dedupe_key or len(dedupe_key) > 300:
            raise ValueError("dedupe_key_invalid")
        controversy = str(raw.get("controversy_status") or "")
        if controversy not in CONTROVERSY_STATUSES:
            raise ValueError("controversy_status_invalid")
        payload = {
            "schema_version": "external-evidence-v1",
            "source_url": source_url,
            "source_type": source_type,
            "published_at": published.isoformat(timespec="seconds"),
            "event_at": event.isoformat(timespec="seconds") if event else None,
            "captured_at": captured.isoformat(timespec="seconds"),
            "symbols": symbols, "industries": industries,
            "direction": direction, "confidence": confidence,
            "expiry": expiry.isoformat(timespec="seconds"),
            "dedupe_key": dedupe_key,
            "primary_source_confirmed": bool(raw.get("primary_source_confirmed")),
            "controversy_status": controversy,
        }
        payload["evidence_id"] = "ere_" + payload_hash({
            "channel": CHANNEL, "dedupe_key": dedupe_key,
        })[:40]
        payload["payload_hash"] = payload_hash(payload)
        return payload

    @staticmethod
    def _proposal_evidence(cur: Any, feature: str) -> tuple[int, int, bool, float | None]:
        cur.execute(
            """SELECT overlay_id,symbol,decision_as_of,base_score,event_adjustment,
                      risk_veto,return_pct FROM external_overlay_outcomes
               WHERE horizon_days=5 AND outcome_status='matured'
                 AND return_pct IS NOT NULL ORDER BY decision_as_of,overlay_id,symbol"""
        )
        rows = list(cur.fetchall())
        rows.sort(key=lambda row: (
            _dt(row[2], "decision_as_of").timestamp(), str(row[0]), str(row[1]),
        ))
        samples = len({(str(row[0]), str(row[1])) for row in rows})
        weeks = len({
            _dt(row[2], "decision_as_of").date().isocalendar()[:2]
            for row in rows
        })
        if samples < MIN_TUNING_SAMPLES:
            return samples, weeks, False, None
        split = max(1, min(len(rows) - 1, int(len(rows) * 0.7)))
        holdout = rows[split:]
        returns = [float(row[6]) for row in holdout]
        delta: float | None = None
        if feature == "event_adjustment":
            adjusted = [float(row[6]) for row in holdout if float(row[4]) != 0]
            unchanged = [float(row[6]) for row in holdout if float(row[4]) == 0]
            if adjusted and unchanged:
                delta = sum(adjusted) / len(adjusted) - sum(unchanged) / len(unchanged)
        elif feature == "risk_veto":
            vetoed = [float(row[6]) for row in holdout if bool(row[5])]
            allowed = [float(row[6]) for row in holdout if not bool(row[5])]
            if vetoed and allowed:
                delta = sum(allowed) / len(allowed) - sum(vetoed) / len(vetoed)
        elif feature == "base_score" and len(returns) >= 3:
            scores = [float(row[3]) for row in holdout]
            score_mean = sum(scores) / len(scores)
            return_mean = sum(returns) / len(returns)
            numerator = sum((score - score_mean) * (ret - return_mean)
                            for score, ret in zip(scores, returns))
            denominator = math.sqrt(
                sum((score - score_mean) ** 2 for score in scores)
                * sum((ret - return_mean) ** 2 for ret in returns)
            )
            if denominator:
                delta = numerator / denominator
        rounded = round(delta, 6) if delta is not None else None
        return samples, weeks, bool(rounded is not None and rounded > 0), rounded

    def save(self, bundle: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        """Validate and atomically save one contemporaneous external overlay."""
        if not actor_id:
            raise PermissionError("actor_required")
        allowed = {
            "channel", "idempotency_key", "selection_run_id", "decision_as_of", "market_regime",
            "external_evidence", "independent_overlay", "news_watchlist",
            "tuning_proposals",
        }
        _exact(bundle, allowed, "external_bundle")
        if bundle.get("channel") != CHANNEL:
            raise ValueError("external_channel_invalid")
        idempotency_key = self._idempotency_key(bundle.get("idempotency_key"))
        request_hash = payload_hash(bundle)
        replay = self._submission_replay(
            idempotency_key=idempotency_key, request_hash=request_hash,
        )
        if replay:
            return replay
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("clock_timezone_required")
        decision = _dt(bundle.get("decision_as_of"), "decision_as_of")
        if decision > now + timedelta(minutes=1) or now - decision > CONTEMPORANEOUS_GRACE:
            raise ValueError("historical_ranking_backfill_forbidden")
        run_id = str(bundle.get("selection_run_id") or "")
        _formal, independent = self._formal_base(run_id)
        base_rows = independent.get("top15") or []
        base = {_symbol(row.get("symbol")): row for row in base_rows}
        if "" in base or len(base) != 15:
            raise ValueError("independent_base_symbols_invalid")

        raw_evidence = bundle.get("external_evidence") or []
        if len(raw_evidence) > 100:
            raise ValueError("external_evidence_limit_exceeded")
        evidence = [self._evidence(dict(row), decision=decision)
                    for row in raw_evidence]
        evidence_by_id = {row["evidence_id"]: row for row in evidence}
        evidence_refs = {
            reference: row["evidence_id"]
            for row in evidence
            for reference in (row["evidence_id"], row["dedupe_key"])
        }

        raw_overlay = bundle.get("independent_overlay") or []
        if len(raw_overlay) != 15:
            raise ValueError("overlay_requires_exact_base_top15")
        overlay_by_symbol: dict[str, dict[str, Any]] = {}
        for raw in raw_overlay:
            raw = dict(raw)
            _exact(raw, {"symbol", "event_adjustment", "risk_veto", "evidence_ids"},
                   "independent_overlay")
            symbol = _symbol(raw.get("symbol"))
            if symbol not in base or symbol in overlay_by_symbol:
                raise ValueError("overlay_membership_must_equal_independent_base")
            adjustment = _finite(raw.get("event_adjustment", 0), "event_adjustment")
            if not MIN_EVENT_ADJUSTMENT <= adjustment <= MAX_EVENT_ADJUSTMENT:
                raise ValueError("event_adjustment_out_of_bounds")
            evidence_ids = list(dict.fromkeys(
                evidence_refs.get(str(item), str(item))
                for item in raw.get("evidence_ids") or []
            ))
            if (adjustment != 0 or bool(raw.get("risk_veto"))) and not evidence_ids:
                raise ValueError("overlay_adjustment_requires_evidence")
            try:
                base_rank = int(base[symbol].get("rank"))
            except (TypeError, ValueError) as exc:
                raise ValueError("independent_base_rank_invalid") from exc
            if base_rank < 1:
                raise ValueError("independent_base_rank_invalid")
            overlay_by_symbol[symbol] = {
                "symbol": symbol,
                "name": str(base[symbol].get("name") or ""),
                "base_rank": base_rank,
                "base_score": round(_finite(
                    base[symbol].get("total_score"), "independent_base_score"
                ), 6),
                "event_adjustment": round(adjustment, 6),
                "risk_veto": bool(raw.get("risk_veto")),
                "evidence_ids": evidence_ids,
            }
        if set(overlay_by_symbol) != set(base):
            raise ValueError("overlay_membership_must_equal_independent_base")
        all_referenced = {item for row in overlay_by_symbol.values()
                          for item in row["evidence_ids"]}

        market_regime = str(bundle.get("market_regime") or "unknown")
        if market_regime not in MARKET_REGIMES:
            raise ValueError("market_regime_invalid")
        locked_at = now.isoformat(timespec="seconds")
        ranking = list(overlay_by_symbol.values())
        for row in ranking:
            row["final_score"] = round(row["base_score"] + row["event_adjustment"], 6)
        ranking.sort(key=lambda row: (row["risk_veto"], -row["final_score"], row["symbol"]))
        for rank, row in enumerate(ranking, 1):
            row["rank"] = rank
        overlay_payload = {
            "schema_version": "independent-overlay-v1", "channel": CHANNEL,
            "selection_run_id": run_id,
            "base_strategy_version": independent["strategy_version"],
            "base_input_snapshot_id": independent.get("input_snapshot_id"),
            "base_market_as_of": independent.get("market_as_of"),
            "decision_as_of": decision.isoformat(timespec="seconds"),
            "ranking_locked_at": locked_at, "market_regime": market_regime,
            "top15": ranking, "top5": ranking[:5],
            "identity_boundary": (
                "external_ranking_of_exact_codex_independent_v1_membership;"
                "formal_and_wencai_not_inputs"
            ),
            "price_authority": "none; use same-snapshot authoritative trade_plan only",
            "auto_apply": False, "auto_execution": False,
        }
        overlay_id = "eio_" + payload_hash(overlay_payload)[:40]
        overlay_payload["overlay_id"] = overlay_id
        overlay_payload["idempotency_key"] = idempotency_key
        overlay_digest = payload_hash(overlay_payload)

        watchlist = []
        for raw in bundle.get("news_watchlist") or []:
            raw = dict(raw)
            _exact(raw, {"symbol", "name", "industry", "reason", "evidence_ids"},
                   "news_watchlist")
            symbol = _symbol(raw.get("symbol"))
            if not symbol or symbol in base:
                raise ValueError("watchlist_must_be_external_to_independent_base")
            reason = str(raw.get("reason") or "").strip()
            if not reason:
                raise ValueError("watchlist_reason_required")
            watch_evidence_ids = list(dict.fromkeys(
                evidence_refs.get(str(item), str(item))
                for item in raw.get("evidence_ids") or []
            ))
            if not watch_evidence_ids:
                raise ValueError("watchlist_evidence_required")
            value = {
                "symbol": symbol, "name": str(raw.get("name") or "")[:100],
                "industry": str(raw.get("industry") or "")[:100],
                "reason": reason[:500],
                "evidence_ids": watch_evidence_ids,
                "quantitative_fields_complete": False,
                "formal_top15_eligible": False, "status": "observation_only",
            }
            watchlist.append(value)
            all_referenced.update(value["evidence_ids"])

        conn = self.store.connect()
        try:
            cur = conn.cursor()
            if all_referenced:
                existing_ids = set()
                for evidence_id in sorted(all_referenced):
                    cur.execute(
                        "SELECT channel FROM external_research_evidence WHERE evidence_id=?",
                        (evidence_id,),
                    )
                    row = cur.fetchone()
                    if row and str(row[0]) == CHANNEL:
                        existing_ids.add(evidence_id)
                if not all_referenced.issubset(set(evidence_by_id) | existing_ids):
                    raise ValueError("overlay_evidence_reference_missing")
            created_at = locked_at
            for row in evidence:
                cur.execute(
                    "SELECT payload_hash FROM external_research_evidence "
                    "WHERE channel=? AND dedupe_key=?",
                    (CHANNEL, row["dedupe_key"]),
                )
                existing = cur.fetchone()
                if existing and str(existing[0]) != row["payload_hash"]:
                    raise ValueError("external_evidence_dedupe_conflict")
                cur.execute(
                    """INSERT INTO external_research_evidence
                       (evidence_id,channel,source_url,source_type,published_at,event_at,
                        captured_at,decision_as_of,symbols,industries,direction,confidence,
                        expiry,dedupe_key,primary_source_confirmed,controversy_status,
                        payload_hash,payload,actor_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(evidence_id) DO NOTHING""",
                    (row["evidence_id"], CHANNEL, row["source_url"], row["source_type"],
                     row["published_at"], row["event_at"], row["captured_at"],
                     decision.isoformat(timespec="seconds"), _encode(row["symbols"]),
                     _encode(row["industries"]), row["direction"], row["confidence"],
                     row["expiry"], row["dedupe_key"],
                     int(row["primary_source_confirmed"]), row["controversy_status"],
                     row["payload_hash"], _encode(row), actor_id, created_at),
                )
            cur.execute(
                """INSERT INTO external_independent_overlays
                   (overlay_id,channel,selection_run_id,base_strategy_version,
                    decision_as_of,ranking_locked_at,market_regime,payload_hash,payload,
                    actor_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(overlay_id) DO NOTHING""",
                (overlay_id, CHANNEL, run_id, independent["strategy_version"],
                 decision.isoformat(timespec="seconds"), locked_at, market_regime,
                 overlay_digest, _encode(overlay_payload), actor_id, created_at),
            )
            for value in watchlist:
                watch_id = "enw_" + payload_hash({
                    "overlay_id": overlay_id, "symbol": value["symbol"],
                })[:40]
                cur.execute(
                    """INSERT INTO external_news_watchlist
                       (watch_id,channel,selection_run_id,decision_as_of,symbol,
                        payload_hash,payload,actor_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(watch_id) DO NOTHING""",
                    (watch_id, CHANNEL, run_id, decision.isoformat(timespec="seconds"),
                     value["symbol"], payload_hash(value), _encode(value), actor_id, created_at),
                )
            proposals = []
            for raw in bundle.get("tuning_proposals") or []:
                raw = dict(raw)
                _exact(raw, {
                    "feature", "mature_sample_count", "covered_weeks",
                    "out_of_sample_delta", "expected_impact", "acceptance_metric",
                    "rollback_condition", "time_split_validated", "status",
                }, "tuning_proposal")
                feature = str(raw.get("feature") or "")[:100]
                if feature not in {"base_score", "event_adjustment", "risk_veto"}:
                    raise ValueError("tuning_feature_invalid")
                if str(raw.get("status") or "proposed") != "proposed":
                    raise ValueError("tuning_status_invalid")
                (actual_samples, actual_weeks, computed_time_split,
                 actual_delta) = self._proposal_evidence(cur, feature)
                time_split = bool(raw.get("time_split_validated")) and computed_time_split
                eligible = (actual_samples >= MIN_TUNING_SAMPLES
                            and actual_weeks >= MIN_TUNING_WEEKS and time_split)
                proposal = {
                    "schema_version": "external-tuning-proposal-v1",
                    "channel": CHANNEL, "feature": feature,
                    "mature_sample_count": actual_samples,
                    "covered_weeks": actual_weeks,
                    "out_of_sample_delta": actual_delta,
                    "submitted_out_of_sample_delta": _finite(
                        raw.get("out_of_sample_delta"), "out_of_sample_delta"
                    ),
                    "expected_impact": str(raw.get("expected_impact") or "")[:500],
                    "acceptance_metric": str(raw.get("acceptance_metric") or "")[:500],
                    "rollback_condition": str(raw.get("rollback_condition") or "")[:500],
                    "time_split_validated": time_split,
                    "status": "review_required" if eligible else "evidence_insufficient",
                    "auto_apply": False,
                }
                proposal_id = "etp_" + payload_hash({
                    "overlay_id": overlay_id, "proposal": proposal,
                })[:40]
                cur.execute(
                    """INSERT INTO strategy_adjustment_proposals
                       (proposal_id,base_policy_hash,evidence_snapshot_id,proposal,
                        validation_status,validation_reason,applied_policy_hash,created_at,applied_at)
                       VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(proposal_id) DO NOTHING""",
                    (proposal_id, independent.get("strategy_hash") or "", overlay_id,
                     _encode(proposal), proposal["status"],
                     None if eligible else "minimum_20_samples_4_weeks_time_split_required",
                     None, created_at, None),
                )
                proposals.append({"proposal_id": proposal_id, **proposal})
            cur.execute(
                "SELECT idempotency_key FROM external_research_submissions WHERE overlay_id=?",
                (overlay_id,),
            )
            overlay_submission = cur.fetchone()
            if overlay_submission and str(overlay_submission[0]) != idempotency_key:
                raise ValueError("external_overlay_already_registered")
            cur.execute(
                """INSERT INTO external_research_submissions
                   (idempotency_key,channel,overlay_id,request_hash,notification_status,
                    notification_consumed_at,actor_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (idempotency_key, CHANNEL, overlay_id, request_hash, "pending",
                 None, actor_id, created_at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        data = {
            "status": "ready", "channel": CHANNEL, "overlay": overlay_payload,
            "evidence": evidence, "news_watchlist": watchlist,
            "tuning_proposals": proposals,
            "idempotency": {
                "key": idempotency_key, "replayed": False,
                "notification_status": "pending",
            },
            "guardrails": {
                "membership_source": "codex-independent-v1 exact top15",
                "event_adjustment_bounds": [MIN_EVENT_ADJUSTMENT, MAX_EVENT_ADJUSTMENT],
                "external_can_publish_formal_selection": False,
                "external_can_create_execution_price": False,
                "minimum_tuning_samples": MIN_TUNING_SAMPLES,
                "minimum_tuning_weeks": MIN_TUNING_WEEKS,
                "human_review_required": True, "auto_apply": False,
                "auto_execution": False,
            },
        }
        return tool_result(
            summary="External independent research overlay saved without changing formal selection.",
            resource_uri=f"shadow://foliant/external-independent/{overlay_id}",
            status="complete", provenance_value=provenance(
                run_id=overlay_id, decision_at=decision.isoformat(timespec="seconds"),
                market_as_of=independent.get("market_as_of"), input_manifest_id=run_id,
            ), data=data, model_payload=data,
        )

    def claim_notification(
        self, *, idempotency_key: str, overlay_id: str,
        notification_slot: str, actor_id: str,
    ) -> dict[str, Any]:
        """Claim one QQ attempt and replay its recorded delivery outcome."""
        if not actor_id:
            raise PermissionError("actor_required")
        key = self._idempotency_key(idempotency_key)
        expected_overlay = str(overlay_id or "").strip()
        if not expected_overlay.startswith("eio_") or len(expected_overlay) != 44:
            raise ValueError("external_overlay_id_invalid")
        slot = str(notification_slot or "").strip()
        if not NOTIFICATION_SLOT_PATTERN.fullmatch(slot):
            raise ValueError("external_notification_slot_invalid")
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            consumed_at = self.clock().isoformat(timespec="seconds")
            cur.execute(
                """SELECT s.overlay_id,s.actor_id,o.decision_as_of
                   FROM external_research_submissions s
                   JOIN external_independent_overlays o ON o.overlay_id=s.overlay_id
                   WHERE s.idempotency_key=? AND s.channel=?""",
                (key, CHANNEL),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("external_submission_missing")
            if str(row[0]) != expected_overlay:
                raise ValueError("external_submission_overlay_mismatch")
            if str(row[1]) != actor_id:
                raise PermissionError("external_submission_actor_mismatch")
            if slot[:10] != str(row[2] or "")[:10]:
                raise ValueError("external_notification_slot_date_mismatch")
            cur.execute(
                """INSERT INTO external_research_notification_claims
                   (idempotency_key,overlay_id,notification_slot,actor_id,consumed_at,
                    delivery_status)
                   VALUES (?,?,?,?,?,'claimed')
                   ON CONFLICT(idempotency_key,notification_slot) DO NOTHING""",
                (key, expected_overlay, slot, actor_id, consumed_at),
            )
            claimed = max(0, int(cur.rowcount or 0)) == 1
            if claimed:
                cur.execute(
                    """UPDATE external_research_submissions
                       SET notification_status='consumed',notification_consumed_at=?
                       WHERE idempotency_key=? AND channel=?""",
                    (consumed_at, key, CHANNEL),
                )
            cur.execute(
                """SELECT overlay_id,actor_id,delivery_status,delivery_attempted_at,
                          delivered_at,delivery_error_code
                   FROM external_research_notification_claims
                   WHERE idempotency_key=? AND notification_slot=?""",
                (key, slot),
            )
            claim_row = cur.fetchone()
            if not claim_row:
                raise RuntimeError("external_notification_claim_missing")
            if str(claim_row[0]) != expected_overlay:
                raise ValueError("external_notification_claim_overlay_mismatch")
            if str(claim_row[1]) != actor_id:
                raise PermissionError("external_notification_claim_actor_mismatch")
            conn.commit()
            delivery_status = str(claim_row[2] or "unknown")
            prior_sent = delivery_status == "delivered"
            replay_status = {
                "delivered": "delivery_replayed",
                "failed": "delivery_replayed",
                "claimed": "delivery_pending",
                "unknown": "delivery_unknown",
            }.get(delivery_status, "delivery_unknown")
            return {
                "status": "claimed" if claimed else replay_status,
                "idempotency_key": key,
                "overlay_id": expected_overlay, "should_send": claimed,
                "notification_slot": slot,
                "prior_sent": prior_sent,
                "sent": prior_sent,
                "delivery_status": delivery_status,
                "delivery_attempted_at": claim_row[3],
                "delivered_at": claim_row[4],
                "delivery_error_code": claim_row[5],
                "delivery_semantics": "at_most_once_per_scheduled_slot_before_qq",
                "auto_execution": False,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_notification_delivery(
        self, *, idempotency_key: str, overlay_id: str,
        notification_slot: str, sent: bool, error_code: Optional[str], actor_id: str,
    ) -> dict[str, Any]:
        """Persist the outcome of the one claimed QQ attempt for safe replay."""
        if not actor_id:
            raise PermissionError("actor_required")
        key = self._idempotency_key(idempotency_key)
        expected_overlay = str(overlay_id or "").strip()
        if not expected_overlay.startswith("eio_") or len(expected_overlay) != 44:
            raise ValueError("external_overlay_id_invalid")
        slot = str(notification_slot or "").strip()
        if not NOTIFICATION_SLOT_PATTERN.fullmatch(slot):
            raise ValueError("external_notification_slot_invalid")
        failure_code = str(error_code or "").strip()[:100] or None
        if not sent and not failure_code:
            raise ValueError("external_notification_delivery_error_required")
        attempted_at = self.clock().isoformat(timespec="seconds")
        delivery_status = "delivered" if sent else "failed"
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT overlay_id,actor_id,delivery_status,delivered_at
                   FROM external_research_notification_claims
                   WHERE idempotency_key=? AND notification_slot=?""",
                (key, slot),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("external_notification_claim_missing")
            if str(row[0]) != expected_overlay:
                raise ValueError("external_notification_claim_overlay_mismatch")
            if str(row[1]) != actor_id:
                raise PermissionError("external_notification_claim_actor_mismatch")
            previous_status = str(row[2] or "unknown")
            if previous_status in {"delivered", "failed"}:
                if previous_status != delivery_status:
                    raise ValueError("external_notification_delivery_conflict")
                conn.commit()
                return {
                    "status": "delivery_replayed", "idempotency_key": key,
                    "overlay_id": expected_overlay, "notification_slot": slot,
                    "sent": previous_status == "delivered",
                    "prior_sent": previous_status == "delivered",
                    "delivery_status": previous_status, "delivered_at": row[3],
                    "auto_execution": False,
                }
            cur.execute(
                """UPDATE external_research_notification_claims
                   SET delivery_status=?,delivery_attempted_at=?,delivered_at=?,
                       delivery_error_code=?
                   WHERE idempotency_key=? AND notification_slot=?""",
                (delivery_status, attempted_at, attempted_at if sent else None,
                 None if sent else failure_code, key, slot),
            )
            cur.execute(
                """UPDATE external_research_submissions
                   SET notification_status=?,notification_consumed_at=?
                   WHERE idempotency_key=? AND channel=?""",
                (delivery_status, attempted_at, key, CHANNEL),
            )
            conn.commit()
            return {
                "status": "recorded", "idempotency_key": key,
                "overlay_id": expected_overlay, "notification_slot": slot,
                "sent": bool(sent), "prior_sent": False,
                "delivery_status": delivery_status,
                "delivery_attempted_at": attempted_at,
                "delivered_at": attempted_at if sent else None,
                "delivery_error_code": None if sent else failure_code,
                "auto_execution": False,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _outcome_summary(self, cur: Any) -> dict[str, Any]:
        cur.execute(
            """SELECT horizon_days,market_regime,base_score,event_adjustment,
                      risk_veto,return_pct FROM external_overlay_outcomes
               WHERE outcome_status='matured' AND return_pct IS NOT NULL
               ORDER BY horizon_days,market_regime"""
        )
        groups: dict[tuple[int, str], list[tuple[float, float, bool, float]]] = {}
        for horizon, regime, base_score, adjustment, veto, ret in cur.fetchall():
            groups.setdefault((int(horizon), str(regime)), []).append(
                (float(base_score), float(adjustment), bool(veto), float(ret))
            )
        rows = []
        for (horizon, regime), values in groups.items():
            base_values = [item[0] for item in values]
            returns = [item[3] for item in values]
            mean_base = sum(base_values) / len(values)
            mean_return = sum(returns) / len(values)
            numerator = sum((x - mean_base) * (y - mean_return)
                            for x, y in zip(base_values, returns))
            denominator = math.sqrt(
                sum((x - mean_base) ** 2 for x in base_values)
                * sum((y - mean_return) ** 2 for y in returns)
            )
            adjusted = [item[3] for item in values if item[1] != 0]
            unchanged = [item[3] for item in values if item[1] == 0]
            vetoed = [item[3] for item in values if item[2]]
            allowed = [item[3] for item in values if not item[2]]
            rows.append({
                "horizon_days": horizon, "market_regime": regime,
                "mature_sample_count": len(values),
                "base_score_return_correlation": (
                    round(numerator / denominator, 6) if denominator else None
                ),
                "event_adjustment_incremental_return_pct": (
                    round(sum(adjusted) / len(adjusted) - sum(unchanged) / len(unchanged), 6)
                    if adjusted and unchanged else None
                ),
                "risk_veto_avoided_loss_pct": (
                    round(sum(allowed) / len(allowed) - sum(vetoed) / len(vetoed), 6)
                    if allowed and vetoed else None
                ),
            })
        return {"horizons": list(HORIZONS), "buckets": rows}

    def latest_data(self) -> dict[str, Any]:
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT overlay_id,payload FROM external_independent_overlays
                   WHERE channel=? ORDER BY ranking_locked_at DESC LIMIT 1""",
                (CHANNEL,),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "status": "missing", "channel": CHANNEL, "overlay": None,
                    "evidence": [], "news_watchlist": [], "tuning_proposals": [],
                    "outcomes": {"horizons": list(HORIZONS), "buckets": []},
                    "auto_apply": False, "auto_execution": False,
                }
            overlay_id, raw_overlay = row
            overlay = _decode(raw_overlay)
            cur.execute(
                """SELECT payload FROM external_news_watchlist
                   WHERE channel=? AND selection_run_id=? AND decision_as_of=?
                   ORDER BY symbol""",
                (CHANNEL, overlay["selection_run_id"], overlay["decision_as_of"]),
            )
            watchlist = [_decode(item[0]) for item in cur.fetchall()]
            evidence_ids = sorted({
                evidence_id
                for item in [*(overlay.get("top15") or []), *watchlist]
                for evidence_id in item.get("evidence_ids") or []
            })
            evidence = []
            for evidence_id in evidence_ids:
                cur.execute(
                    "SELECT payload FROM external_research_evidence WHERE evidence_id=?",
                    (evidence_id,),
                )
                evidence_row = cur.fetchone()
                if evidence_row:
                    evidence.append(_decode(evidence_row[0]))
            cur.execute(
                """SELECT proposal_id,proposal,validation_status,validation_reason
                   FROM strategy_adjustment_proposals WHERE evidence_snapshot_id=?
                   ORDER BY created_at,proposal_id""",
                (overlay_id,),
            )
            proposals = []
            for proposal_id, proposal, status, reason in cur.fetchall():
                proposals.append({"proposal_id": proposal_id, **_decode(proposal),
                                  "status": status, "validation_reason": reason})
            outcomes = self._outcome_summary(cur)
            return {
                "status": "ready", "channel": CHANNEL, "overlay": overlay,
                "evidence": evidence, "news_watchlist": watchlist,
                "tuning_proposals": proposals, "outcomes": outcomes,
                "identity_boundary": overlay.get("identity_boundary"),
                "auto_apply": False, "auto_execution": False,
            }
        finally:
            conn.close()

    def latest(self) -> dict[str, Any]:
        data = self.latest_data()
        overlay = data.get("overlay") or {}
        return tool_result(
            summary=("External independent research is available."
                     if data["status"] == "ready" else
                     "No external independent research has been saved."),
            resource_uri=(f"shadow://foliant/external-independent/{overlay.get('overlay_id')}"
                          if overlay else "shadow://foliant/external-independent/missing"),
            status="complete" if data["status"] == "ready" else "missing",
            provenance_value=provenance(
                run_id=overlay.get("overlay_id") or "external-independent-missing",
                decision_at=overlay.get("decision_as_of"),
                market_as_of=overlay.get("base_market_as_of"),
            ), data=data, model_payload=data,
        )

    def rollback_proposal(self, proposal_id: str) -> dict[str, Any]:
        """Mark a proposal rolled back without ever applying a policy hash."""
        conn = self.store.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """UPDATE strategy_adjustment_proposals
                   SET validation_status='rolled_back',applied_policy_hash=NULL,applied_at=NULL
                   WHERE proposal_id=? AND validation_status IN
                       ('review_required','evidence_insufficient')""",
                (str(proposal_id),),
            )
            changed = max(0, int(cur.rowcount or 0))
            conn.commit()
            return {"proposal_id": str(proposal_id),
                    "status": "rolled_back" if changed else "unchanged",
                    "applied_policy_hash": None, "auto_apply": False}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def settle_outcomes(self, *, limit: int = 100) -> dict[str, int]:
        """Evaluate research-only close returns at 1/3/5/10/20 trading days."""
        conn = self.store.connect()
        inserted = 0
        pending = 0
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT overlay_id,decision_as_of,market_regime,payload
                   FROM external_independent_overlays ORDER BY decision_as_of LIMIT ?""",
                (max(1, min(500, int(limit))),),
            )
            for overlay_id, decision_as_of, regime, raw in cur.fetchall():
                overlay = _decode(raw)
                market_as_of = str(overlay.get("base_market_as_of") or "")[:10]
                for item in overlay.get("top15") or []:
                    symbol = _symbol(item.get("symbol"))
                    cur.execute(
                        """SELECT trade_date,close FROM research_daily_bars
                           WHERE symbol=? AND adjustment='qfq' AND trade_date>=?
                             AND close IS NOT NULL AND quality_status NOT IN ('failed','unknown_unit')
                           ORDER BY trade_date LIMIT 21""",
                        (symbol, market_as_of),
                    )
                    bars = [(str(day), float(close)) for day, close in cur.fetchall()
                            if float(close or 0) > 0]
                    for horizon in HORIZONS:
                        if len(bars) <= horizon:
                            pending += 1
                            continue
                        return_pct = round((bars[horizon][1] / bars[0][1] - 1) * 100, 6)
                        cur.execute(
                            """INSERT INTO external_overlay_outcomes
                               (overlay_id,symbol,horizon_days,decision_as_of,market_regime,
                                base_score,event_adjustment,risk_veto,return_pct,
                                outcome_status,evaluated_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(overlay_id,symbol,horizon_days) DO NOTHING""",
                            (overlay_id, symbol, horizon, decision_as_of, regime,
                             float(item.get("base_score") or 0),
                             float(item.get("event_adjustment") or 0),
                             int(bool(item.get("risk_veto"))), return_pct,
                             "matured", now_iso()),
                        )
                        inserted += max(0, int(cur.rowcount or 0))
            conn.commit()
            return {"inserted": inserted, "pending": pending}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
