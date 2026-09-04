"""Runtime-model telemetry without prompts, outputs, credentials or reasoning."""
from contextvars import ContextVar
from datetime import datetime, timezone
from queue import Queue, Empty, Full
import time
import uuid

_invocations = Queue(maxsize=512)
_trace = ContextVar("research_model_trace", default=None)


def begin():
    identity = uuid.uuid4().hex
    _trace.set(identity)
    return identity, time.monotonic()


def record(identity, started, *, provider, model, fallback_index, status, call_type, usage=None):
    value = {"object_id": f"{identity}:{fallback_index}", "call_id": identity, "provider": provider,
             "model": model, "fallback_index": fallback_index, "status": status, "call_type": call_type,
             "completed_at": datetime.now(timezone.utc).isoformat(), "elapsed_ms": round((time.monotonic() - started) * 1000),
             "usage": usage or {}, "content_recorded": False, "observer": "foliant_router"}
    value["tool_contract_version"] = "research-tools-v1"
    try:
        _invocations.put_nowait(value)
    except Full:
        pass  # Best-effort telemetry is never a control-plane authorization.


def flush(store, limit=128):
    from data.reliability_store import ReliabilityStore
    repo = ReliabilityStore(store)
    count = 0
    for _ in range(limit):
        try:
            value = _invocations.get_nowait()
        except Empty:
            break
        repo.once("model_invocation", value["object_id"], value)
        count += 1
    return {"persisted": count, "scope": "foliant_router_only; bounded_best_effort"}


def historical_prediction_allowed(*, generated_at, decision_at, deterministic_extraction=False):
    if deterministic_extraction:
        return {"allowed": False, "classification": "reconstructed_extraction_not_prediction"}
    return {"allowed": datetime.fromisoformat(generated_at) <= datetime.fromisoformat(decision_at),
            "classification": "contemporaneous" if generated_at <= decision_at else "historical_backfill"}
