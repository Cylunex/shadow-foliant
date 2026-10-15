"""Deterministic checks of declared research evidence, never a truth oracle.

Arithmetic, time eligibility and textual entailment are separate. Source fields
are declarations, not proof that a source was fetched or that prose is entailed.
This module performs no I/O and never trusts persisted/client verification flags.
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re

from application.results import payload_hash
from data.reliability_store import canonical_time


CLAIM_FIELDS = ("text", "passages", "calculation", "expires_at")
SEMANTIC_FIELDS = ("unit", "currency", "basis", "period_kind")
SCHEMA = "research-claim-verification-v1"
# Consume an entire numeric expression first: a comma, exponent or sign must
# never let a suffix such as 200 in 1,200 masquerade as a separately cited value.
_CANDIDATE = re.compile(
    r"(?<![A-Za-z\d_.+–—-])[+-]?(?:[0-9](?:[0-9,]*[0-9])?(?:\.[0-9]*)?|\.[0-9]+)"
    r"(?:[eE][+-]?[0-9]+)?(?![A-Za-z\d_.])"
)
_NUMERIC_TRANSLATION = str.maketrans({"−": "-", "－": "-", "＋": "+", "，": ","})
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_GROUPED = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?\Z")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_numeric_literal")
    token = str(value).translate(_NUMERIC_TRANSLATION)
    if len(token) > 64 or sum(c.isdigit() for c in token) > 40:
        raise ValueError("numeric_literal_too_large")
    if not (_DECIMAL.fullmatch(token) or _GROUPED.fullmatch(token)):
        raise ValueError("invalid_numeric_literal")
    return Decimal(token.replace(",", ""))


def _quoted_numbers(quote):
    quote = quote.translate(_NUMERIC_TRANSLATION)
    numbers = set()
    for match in _CANDIDATE.finditer(quote):
        try:
            number = _number(match.group())
        except ValueError:
            continue
        before = quote[:match.start()].rstrip().rstrip("$€£¥￥").rstrip()
        after = quote[match.end():].lstrip()
        # Accounting parentheses denote a negative amount. A separately stated
        # negative sign inside parentheses is ambiguous, so do not infer it.
        if before.endswith(("(", "（")) and after.startswith((")", "）")):
            if match.group().startswith(("+", "-", "−")):
                continue
            number = number.copy_negate()
        numbers.add(number)
    return numbers


def _time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp_required")
    try:
        return canonical_time(value)
    except ValueError as exc:
        if str(exc) == "timezone_required":
            raise
        raise ValueError("timestamp_invalid") from exc


def compile_claim(text, passages, *, calculation=None, expires_at, as_of=None):
    """Validate original inputs; only the service may choose the cutoff."""
    if (not isinstance(text, str) or not text.strip() or len(text) > 4000
            or not isinstance(passages, list) or not 1 <= len(passages) <= 12):
        raise ValueError("claim_requires_bounded_evidence")
    cutoff = _time(as_of if as_of is not None else datetime.now(timezone.utc).isoformat())
    expires_at = _time(expires_at)
    passages = deepcopy(passages)
    for passage in passages:
        if not isinstance(passage, dict) or not all(
            isinstance(passage.get(k), str) and passage[k].strip()
            for k in ("source_id", "quote", "published_at", "first_seen_at", "locator")
        ):
            raise ValueError("passage_provenance_required")
        if any(len(passage[k]) > 4000 for k in ("source_id", "quote", "locator")):
            raise ValueError("passage_too_large")
        published, seen = (_time(passage[k]) for k in ("published_at", "first_seen_at"))
        if datetime.fromisoformat(published) > datetime.fromisoformat(seen):
            raise ValueError("passage_observed_before_publication")
        if datetime.fromisoformat(seen) > datetime.fromisoformat(cutoff):
            raise ValueError("post_decision_evidence_forbidden")
        passage.update(published_at=published, first_seen_at=seen)

    result = None
    if calculation is not None:
        if not isinstance(calculation, dict):
            raise ValueError("invalid_calculation")
        operands = calculation.get("operands")
        if not isinstance(operands, list) or not 1 <= len(operands) <= 12:
            raise ValueError("bounded_operands_required")
        values, semantics = [], set()
        for operand in operands:
            index = operand.get("passage_index") if isinstance(operand, dict) else None
            if type(index) is not int or not 0 <= index < len(passages):
                raise ValueError("operand_source_required")
            passage = passages[index]
            definition = tuple(passage.get(k) for k in SEMANTIC_FIELDS)
            if not all(isinstance(v, str) and v.strip() and len(v) <= 100 for v in definition):
                raise ValueError("calculation_semantics_required")
            semantics.add(definition)
            number = _number(operand.get("value"))
            if number not in _quoted_numbers(passage["quote"]):
                raise ValueError("operand_not_in_passage")
            values.append(number)
        if len(semantics) != 1:
            raise ValueError("incompatible_calculation_semantics")
        with localcontext() as context:
            context.prec = 96
            operation = calculation.get("operation")
            if operation == "sum":
                result = sum(values, Decimal(0))
            elif operation == "difference" and len(values) == 2:
                result = values[0] - values[1]
            elif operation in {"ratio", "growth_pct"} and len(values) == 2 and values[1]:
                result = values[0] / values[1]
                if operation == "growth_pct":
                    result = (result - 1) * 100
            else:
                raise ValueError("unsupported_calculation")
            if "expected" in calculation and _number(calculation["expected"]) != result:
                raise ValueError("claim_number_mismatch")
    verification = {
        "schema_version": SCHEMA, "as_of": cutoff,
        "provenance": "declared", "source_authenticity": "not_verified",
        "temporal": "eligible", "arithmetic": "verified" if result is not None else "not_requested",
        "semantics": "declared_compatible" if result is not None else "not_checked",
        "entity_period_alignment": "not_verified", "entailment": "unverified",
        "reasons": ["source_authenticity_unverified", "semantic_entailment_unverified"],
    }
    value = {"text": text, "passages": passages, "calculation": deepcopy(calculation),
             "computed_value": str(result) if result is not None else None,
             "state": "unverified", "expires_at": expires_at, "evidence_as_of": cutoff,
             "semantic_entailment": "not_automatically_proven", "verification": verification}
    value["evidence_hash"] = payload_hash({k: value[k] for k in (*CLAIM_FIELDS, "evidence_as_of")})
    return value


def assess_claim(claim, *, now, as_of=None):
    """Recheck immutable input, including legacy rows, without modifying it."""
    clock = _time(now)
    check = {"schema_version": SCHEMA, "arithmetic": "not_verified", "temporal": "unknown",
             "provenance": "not_verified", "source_authenticity": "not_verified",
             "semantics": "not_checked", "entity_period_alignment": "not_verified",
             "entailment": "unverified", "computed_value": None, "reasons": [], "freshness": "stale"}
    try:
        if not isinstance(claim, dict):
            raise ValueError("invalid_claim")
        expiry = _time(claim.get("expires_at"))
        check["freshness"] = "current" if datetime.fromisoformat(expiry) > datetime.fromisoformat(clock) else "stale"
        # The containing revision's server timestamp is authoritative. A stored
        # claim timestamp can only tighten it, never move the boundary forward.
        cutoffs = [_time(v) for v in (as_of, claim.get("evidence_as_of")) if v is not None]
        cutoff = min([clock, *cutoffs], key=datetime.fromisoformat)
        compiled = compile_claim(**{k: claim[k] for k in CLAIM_FIELDS if k in claim}, as_of=cutoff)
        check.update(compiled["verification"], computed_value=compiled["computed_value"])
        if not cutoffs:
            check["temporal"] = "unknown"
            check["reasons"].append("evidence_cutoff_unknown")
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        code = str(exc) if isinstance(exc, ValueError) else "invalid_claim"
        check["reasons"] = [code]
        if code in {"passage_observed_before_publication", "post_decision_evidence_forbidden"}:
            check["temporal"] = "invalid"
    if check["freshness"] == "stale":
        check["reasons"].append("claim_expired_or_expiry_invalid")
    return check


def validity(claims, *, now, as_of=None):
    checks = [{"index": i, **assess_claim(claim, now=now, as_of=as_of)} for i, claim in enumerate(claims)]
    contradicted = any(isinstance(c, dict) and c.get("state") == "contradicted" for c in claims)
    return {
        "schema_version": SCHEMA,
        "coverage": 0,  # Textual support is never established by arithmetic.
        "arithmetic_coverage": sum(c["arithmetic"] == "verified" for c in checks) / len(checks) if checks else 0,
        "freshness": "current" if checks and all(c["freshness"] == "current" for c in checks) else "stale",
        "conclusion": "contradicted" if contradicted else "unverified",
        "use": "review_required", "claim_checks": checks,
    }
