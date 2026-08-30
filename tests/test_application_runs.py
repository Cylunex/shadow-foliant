from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from application.outbox import OutboxPublisher
from application.results import provenance, tool_result
from application.run_repository import IdempotencyConflict, RunRepository
from application.services import (
    ApplicationError,
    ResearchRunQueryService,
    RunCoordinator,
    SelectionRunService,
)


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture()
def repository(tmp_path):
    database = tmp_path / "runs.db"

    def connect(_name=""):
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        return conn

    return RunRepository(connect_fn=connect, is_postgres=False)


def _result(run_id: str):
    return tool_result(
        summary="example complete",
        resource_uri=f"shadow://foliant/runs/{run_id}",
        status="complete",
        provenance_value=provenance(
            run_id=run_id,
            market_as_of="2026-08-21",
            financial_cutoff_at="2026-08-22T09:00:00+08:00",
            universe_snapshot_id="universe-example",
            input_manifest_id="manifest-example",
            policy_hash="policy-example",
            code_revision="revision-example",
        ),
        data={"items": list(range(120))},
    )


def test_idempotent_preview_run_persists_result_and_outbox(repository) -> None:
    coordinator = RunCoordinator(repository, executor=InlineExecutor(), max_active_per_actor=2)
    first = coordinator.submit(
        actor_id="agent-example",
        capability="foliant.selection.preview",
        run_kind="selection",
        idempotency_key="selection-example-key",
        request_payload={"selection_date": "2026-08-22", "mode": "preview"},
        request_id="request-example",
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        event_type="foliant.selection.completed",
        runner=_result,
    )
    repeated = coordinator.submit(
        actor_id="agent-example",
        capability="foliant.selection.preview",
        run_kind="selection",
        idempotency_key="selection-example-key",
        request_payload={"selection_date": "2026-08-22", "mode": "preview"},
        request_id="request-retry",
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        event_type="foliant.selection.completed",
        runner=lambda _run_id: pytest.fail("idempotent retry must not execute again"),
    )

    assert first["run_id"] == repeated["run_id"]
    stored = repository.get(first["run_id"])
    assert stored["status"] == "complete"
    assert stored["mode"] == "preview"
    assert stored["result_payload"]["provenance"]["input_manifest_id"] == "manifest-example"
    with repository.connect() as conn:
        event = conn.execute(
            "SELECT event_type,payload FROM foliant_domain_outbox WHERE run_id=?",
            (first["run_id"],),
        ).fetchone()
    assert event[0] == "foliant.selection.completed"
    assert "items" not in event[1]


def test_same_idempotency_key_with_different_payload_conflicts(repository) -> None:
    repository.create_or_get(
        actor_id="agent-example",
        capability="foliant.backtest.preview",
        run_kind="backtest",
        idempotency_key="backtest-example-key",
        request_payload={"symbols": ["600519"]},
        resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
    )
    with pytest.raises(IdempotencyConflict):
        repository.create_or_get(
            actor_id="agent-example",
            capability="foliant.backtest.preview",
            run_kind="backtest",
            idempotency_key="backtest-example-key",
            request_payload={"symbols": ["000858"]},
            resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
        )


def test_run_query_is_creator_scoped_and_large_list_is_paginated(repository) -> None:
    created = repository.create_or_get(
        actor_id="agent-owner",
        capability="foliant.backtest.preview",
        run_kind="backtest",
        idempotency_key="backtest-owner-key",
        request_payload={"symbols": ["600519"]},
        resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
    ).run
    assert repository.mark_running(created["run_id"])
    repository.complete(created["run_id"], _result(created["run_id"]),
                        event_type="foliant.backtest.completed")
    query = ResearchRunQueryService(repository)

    with pytest.raises(ApplicationError) as denied:
        query.get(created["run_id"], actor_id="another-agent")
    assert denied.value.status_code == 404

    page = query.result(created["run_id"], actor_id="agent-owner", limit=25)
    assert len(page["data"]["items"]) == 25
    assert page["continuation"] == {
        "field": "items", "offset": 0, "next_offset": 25, "limit": 25, "total": 120,
    }
    tail = query.result(
        created["run_id"], actor_id="agent-owner", offset=50, limit=100
    )
    assert tail["data"]["items"] == list(range(50, 120))
    assert tail["continuation"]["next_offset"] is None


def test_cancelled_queue_and_stale_restart_recovery(repository) -> None:
    queued = repository.create_or_get(
        actor_id="agent-owner",
        capability="foliant.selection.preview",
        run_kind="selection",
        idempotency_key="cancel-example-key",
        request_payload={"selection_date": "2026-08-22"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
    ).run
    assert repository.cancel(queued["run_id"], "agent-owner")
    assert repository.get(queued["run_id"])["status"] == "cancelled"

    stale = repository.create_or_get(
        actor_id="agent-owner",
        capability="foliant.selection.preview",
        run_kind="selection",
        idempotency_key="stale-example-key",
        request_payload={"selection_date": "2026-08-23"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
    ).run
    assert repository.mark_running(stale["run_id"])
    old = (datetime.now().astimezone() - timedelta(hours=8)).isoformat(timespec="seconds")
    with repository.connect() as conn:
        conn.execute("UPDATE foliant_runs SET updated_at=? WHERE run_id=?", (old, stale["run_id"]))
        conn.commit()
    assert repository.recover_incomplete(stale_seconds=3600) == 1
    recovered = repository.get(stale["run_id"])
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "worker_restarted"


def test_selection_preview_never_persists_or_uses_wencai(repository, monkeypatch) -> None:
    coordinator = RunCoordinator(repository, executor=InlineExecutor(), max_active_per_actor=2)
    calls = []

    def fake_run(_self, selected, **kwargs):
        calls.append((selected, kwargs))
        return {
            "status": "success", "candidates": [{"code": "600519"}],
            "metadata": {"market_as_of": selected, "policy_hash": "policy-example"},
            "input_manifest": {"manifest_id": "manifest-example"},
        }

    monkeypatch.setattr("analysis.local_stock_selector.LocalStockSelector.run", fake_run)
    service = SelectionRunService(coordinator, store=object())
    created = service.create_preview(
        selection_date="2026-08-21", decision_mode="postclose",
        actor_id="agent-example", idempotency_key="selection-preview-example",
    )

    assert repository.get(created["run_id"])["status"] == "complete"
    assert calls == [("2026-08-21", {
        "decision_mode": "postclose", "wencai_reference": None, "persist": False,
    })]


def test_formal_selection_read_performs_no_write_or_reanalysis() -> None:
    class ReadOnlyStore:
        def __init__(self):
            self.calls = []

        def latest_formal_selection(self):
            self.calls.append("latest_formal_selection")

        def __getattr__(self, name):
            raise AssertionError(f"read path attempted unexpected operation: {name}")

    store = ReadOnlyStore()
    result = SelectionRunService(store=store).latest_formal()
    assert result["status"] == "missing"
    assert store.calls == ["latest_formal_selection"]


def test_formal_selection_read_exposes_lanes_and_reference_layers() -> None:
    class Store:
        def latest_formal_selection(self):
            return {
                "run_id": "selection-1", "selection_date": "2026-08-24",
                "metadata": {"lane_counts": {"core": 3, "satellite": 1, "timing": 1}},
                "comparison": {"overlap": ["600001"]},
                "artifacts": {
                    "formal_top15": {"payload": [{
                        "code": "600001", "rank": 1, "assigned_lane": "core",
                    }]},
                    "formal_top5": {"payload": [{
                        "code": "600001", "rank": 1, "assigned_lane": "core",
                    }]},
                    "display_overlay": {"payload": [{
                        "code": "600001", "name": "样例", "price": 10,
                    }]},
                    "local_strategy_nominations": {"payload": {"strategies": {}}},
                    "genome_nominations": {"payload": {"status": "ready"}},
                    "fusion_policy": {"payload": {"version": "local-fusion-v1"}},
                    "wencai_strategy_runs": {"payload": {"strategies": {"低估值": {}}}},
                    "miaoxiang_strategy_runs": {"payload": {"strategies": {"妙想·低估值": {}}}},
                },
            }

    result = SelectionRunService(store=Store()).latest_formal()
    assert result["status"] == "complete"
    assert result["data"]["formal_top5"][0]["name"] == "样例"
    assert result["data"]["formal_top5"][0]["assigned_lane"] == "core"
    assert result["data"]["lane_counts"]["core"] == 3
    assert "wencai" in result["data"]["references"]
    assert "miaoxiang" in result["data"]["references"]


def test_durable_worker_lease_reclaims_once_then_fails_after_attempt_budget(repository) -> None:
    created = repository.create_or_get(
        actor_id="agent-owner", capability="foliant.selection.preview",
        run_kind="selection", idempotency_key="durable-lease-example",
        request_payload={"selection_date": "2026-08-21", "decision_mode": "postclose"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        max_active=2, max_attempts=2,
    ).run
    first = repository.claim_next("worker-one", lease_seconds=30)
    assert first["run_id"] == created["run_id"]
    assert first["attempt"] == 1
    with repository.connect() as conn:
        conn.execute(
            "UPDATE foliant_runs SET lease_until=? WHERE run_id=?",
            ("2000-01-01T00:00:00+00:00", created["run_id"]),
        )
        conn.commit()
    second = repository.claim_next("worker-two", lease_seconds=30)
    assert second["run_id"] == created["run_id"]
    assert second["attempt"] == 2
    with repository.connect() as conn:
        conn.execute(
            "UPDATE foliant_runs SET lease_until=? WHERE run_id=?",
            ("2000-01-01T00:00:00+00:00", created["run_id"]),
        )
        conn.commit()
    assert repository.claim_next("worker-three", lease_seconds=30) is None
    assert repository.get(created["run_id"])["error_code"] == "worker_lease_expired"


def test_quota_check_and_create_share_one_repository_transaction(repository) -> None:
    repository.create_or_get(
        actor_id="agent-owner", capability="foliant.selection.preview",
        run_kind="selection", idempotency_key="quota-first-example",
        request_payload={"selection_date": "2026-08-21"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        max_active=1,
    )
    from application.run_repository import RunQuotaExceeded

    with pytest.raises(RunQuotaExceeded):
        repository.create_or_get(
            actor_id="agent-owner", capability="foliant.backtest.preview",
            run_kind="backtest", idempotency_key="quota-second-example",
            request_payload={"symbols": ["600519"]},
            resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
            max_active=1,
        )


def test_terminal_failure_cannot_be_overwritten_by_late_completion(repository) -> None:
    created = repository.create_or_get(
        actor_id="agent-owner", capability="foliant.backtest.preview",
        run_kind="backtest", idempotency_key="late-result-example",
        request_payload={"mode": "preview"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/backtests/{run_id}",
    ).run
    assert repository.mark_running(created["run_id"])
    repository.fail(created["run_id"], "execution_timeout")
    repository.complete(
        created["run_id"], _result(created["run_id"]),
        event_type="foliant.backtest.completed",
    )
    assert repository.get(created["run_id"])["status"] == "failed"
    assert repository.pending_outbox() == []


def test_reclaimed_worker_fence_rejects_late_progress_and_completion(repository) -> None:
    created = repository.create_or_get(
        actor_id="agent-owner", capability="foliant.selection.preview",
        run_kind="selection", idempotency_key="fence-example",
        request_payload={"selection_date": "2026-08-21"},
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        max_attempts=2,
    ).run
    first = repository.claim_next("worker-one", lease_seconds=30)
    assert first["fencing_token"]
    assert repository.append_progress(
        created["run_id"], worker_id="worker-one", fencing_token=first["fencing_token"],
        phase="loading_inputs",
    )
    with repository.connect() as conn:
        conn.execute(
            "UPDATE foliant_runs SET lease_until=? WHERE run_id=?",
            ("2000-01-01T00:00:00+00:00", created["run_id"]),
        )
        conn.commit()
    second = repository.claim_next("worker-two", lease_seconds=30)
    assert second["fencing_token"] != first["fencing_token"]
    assert not repository.append_progress(
        created["run_id"], worker_id="worker-one", fencing_token=first["fencing_token"],
        phase="publishing",
    )
    assert not repository.complete(
        created["run_id"], _result(created["run_id"]),
        event_type="foliant.selection.completed", worker_id="worker-one",
        fencing_token=first["fencing_token"],
    )
    assert repository.get(created["run_id"])["status"] == "running"
    assert [item["phase"] for item in repository.progress(created["run_id"])] == [
        "loading_inputs"
    ]


def test_outbox_publisher_marks_only_successful_metadata_events(repository) -> None:
    coordinator = RunCoordinator(repository, executor=InlineExecutor())
    created = coordinator.submit(
        actor_id="agent-example", capability="foliant.selection.preview",
        run_kind="selection", idempotency_key="outbox-example-key",
        request_payload={"mode": "preview"}, request_id="request-example",
        resource_uri_factory=lambda run_id: f"shadow://foliant/selection-runs/{run_id}",
        event_type="foliant.selection.completed", runner=_result,
    )
    delivered = []
    publisher = OutboxPublisher(repository, lambda name, payload: delivered.append((name, payload)))
    assert publisher.publish_once() == 1
    assert delivered[0][0] == "foliant.selection.completed"
    assert "result" not in delivered[0][1]
    assert repository.pending_outbox() == []
    assert repository.get(created["run_id"])["status"] == "complete"
