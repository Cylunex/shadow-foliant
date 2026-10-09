from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from jobs import intraday_decision_monitor as monitor
from notify.archive_gateway import archived_call
from notify import notification_router


TZ = ZoneInfo("Asia/Shanghai")


def row(now, *, price=10.0, stop=9.0, action="sell", source="hard_risk",
        plan_day="2026-10-08", sellable=100, tradeable=True):
    return {"symbol": "600001", "name": "示例持仓", "quantity": 100,
            "sellable_quantity": sellable, "sellability_source": "broker",
            "sellability_as_of": now.date().isoformat(), "tradeable": tradeable,
            "price": price, "stop_loss": stop, "plan_as_of": plan_day,
            "plan_valid_until": (now + timedelta(hours=6)).isoformat(),
            "plan_adjustment": "qfq", "quote_adjustment": "qfq",
            "selection_run_id": "formal-run-1",
            "plan_available": True, "price_basis": "trade_plan；formal_manifest_qfq",
            "price_actionable": True, "quote_provider": "provider",
            "quote_time_source": "provider", "quote_as_of": now.isoformat(),
            "action": action, "decision_source": source,
            "reason": "当前价触及 trade_plan 止损"}


def cycle(current, previous, now):
    policy, events = monitor._critical_stop_policy(
        [current], previous, now,
        {"run_id": "formal-run-1", "metadata": {"market_as_of": current["plan_as_of"]}})
    return {"holdings": [current], "critical_notification_policy": policy}, events


def test_no_verified_critical_event_is_silent_and_old_breach_is_baselined():
    now = datetime(2026, 10, 9, 10, 5, tzinfo=TZ)
    breached = row(now, price=8.8)
    state, events = cycle(breached, {}, now)
    assert not events and state["critical_notification_policy"]["eligible_new_event_count"] == 0
    state, events = cycle(row(now + timedelta(minutes=20), price=8.7), state,
                          now + timedelta(minutes=20))
    assert not events
    assert state["critical_notification_policy"]["active_level"] != "none"


def test_two_distinct_recovery_quotes_then_one_new_hard_stop():
    now = datetime(2026, 10, 9, 9, 40, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    for minute in (20, 40):
        stamp = now + timedelta(minutes=minute)
        state, events = cycle(row(stamp, price=9.2), state, stamp)
        assert not events
    # Replaying the same quote cannot manufacture a second recovery observation.
    state, _ = cycle(row(now + timedelta(minutes=40), price=9.2), state,
                     now + timedelta(minutes=40))
    stamp = now + timedelta(minutes=60)
    state, events = cycle(row(stamp, price=8.9), state, stamp)
    assert len(events) == 1
    assert re.fullmatch(r"critical-stop:600001:[0-9a-f]{12}:sell:1",
                        events[0]["idempotency_key"])
    title, body = monitor.format_critical_alert(events[0],
                                                {"holdings": [events[0]["item"]]})
    assert "最高级止损风险" in title
    for value in ("示例持仓（600001）", "最终判断：卖出复核", "同批现价", "权威止损",
                  "原因：", "行情时点：", "失效条件："):
        assert value in body
    assert len(body.splitlines()) <= 6
    stamp += timedelta(minutes=20)
    state, events = cycle(row(stamp, price=8.8), state, stamp)
    assert not events


def test_invalid_or_boundary_quote_resets_recovery_and_guards_block_action():
    now = datetime(2026, 10, 9, 9, 40, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    stamp = now + timedelta(minutes=20)
    state, _ = cycle(row(stamp, price=9.2), state, stamp)
    stamp += timedelta(minutes=20)
    invalid = row(stamp, price=9.2)
    invalid["price_actionable"] = False
    state, _ = cycle(invalid, state, stamp)
    assert state["critical_notification_policy"]["states"]["600001"]["recovery_count"] == 0
    stamp += timedelta(minutes=20)
    state, _ = cycle(row(stamp, price=9.05), state, stamp)
    assert state["critical_notification_policy"]["states"]["600001"]["recovery_count"] == 0
    for minute in (80, 100):
        stamp = now + timedelta(minutes=minute)
        state, _ = cycle(row(stamp, price=9.2), state, stamp)
    stamp += timedelta(minutes=20)
    state, events = cycle(row(stamp, price=8.9, action="hold"), state, stamp)
    assert not events
    stamp += timedelta(minutes=20)
    state, events = cycle(row(stamp, price=8.8), state, stamp)
    assert not events  # Guarded hold cannot become a delayed sell alert.


def test_batch_receipt_time_cannot_arm_a_critical_stop():
    now = datetime(2026, 10, 9, 9, 40, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    for minute in (20, 40):
        stamp = now + timedelta(minutes=minute)
        receipt_only = row(stamp, price=9.2)
        receipt_only["quote_time_source"] = "batch_retrieved_at"
        state, events = cycle(receipt_only, state, stamp)
        assert not events
    assert state["critical_notification_policy"]["states"]["600001"]["armed"] is False
    stamp = now + timedelta(minutes=60)
    state, events = cycle(row(stamp, price=8.9), state, stamp)
    assert not events

    assessed = monitor.assess_quotes(
        [{"symbol": "600001"}],
        {"600001": {"price": 9.2, "retrieved_at": stamp.isoformat(),
                    "source": "provider"}},
        stamp,
    )
    assert assessed["items"]["600001"]["quote_time_source"] == "retrieved_at"
    invalid = monitor.assess_quotes(
        [{"symbol": "600001"}],
        {"600001": {"price": 9.2, "quote_time": "invalid",
                    "quote_time_source": "provider", "source": "provider"}},
        stamp,
    )
    assert invalid["items"]["600001"]["quote_time_source"] == (
        "retrieved_at_invalid_provider_time")


def test_future_provider_quote_cannot_trigger_critical_stop():
    now = datetime(2026, 10, 9, 9, 40, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    for minute in (20, 40):
        stamp = now + timedelta(minutes=minute)
        state, _ = cycle(row(stamp, price=9.2), state, stamp)
    stamp = now + timedelta(minutes=60)
    future = row(stamp, price=8.9)
    future["quote_as_of"] = (stamp + timedelta(minutes=15)).isoformat()
    state, events = cycle(future, state, stamp)
    assert not events


def test_missing_sellability_fails_closed_and_plan_replacement_changes_identity():
    now = datetime(2026, 10, 9, 9, 40, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    for minute in (20, 40):
        stamp = now + timedelta(minutes=minute)
        state, _ = cycle(row(stamp, price=9.2), state, stamp)
    stamp += timedelta(minutes=20)
    state, events = cycle(row(stamp, price=8.9, sellable=None), state, stamp)
    assert not events
    assert state["critical_notification_policy"]["silent_reasons"][
        "sellability_or_trading_unverified"] == 1
    # The holding disappears for a cycle; its episode counter remains stored.
    old = state["critical_notification_policy"]["states"]["600001"]
    policy, _ = monitor._critical_stop_policy([], state, stamp + timedelta(minutes=20))
    assert policy["states"]["600001"]["episode"] == old["episode"]
    stamp += timedelta(minutes=40)
    state = {"holdings": [], "critical_notification_policy": policy}
    state, events = cycle(row(stamp, price=8.9, plan_day="2026-10-09"), state, stamp)
    assert not events  # New plan is a baseline, even at the same breached price.
    new_epoch = state["critical_notification_policy"]["states"]["600001"]["epoch"]
    assert new_epoch != old["epoch"]
    for minute in (20, 40):
        next_stamp = stamp + timedelta(minutes=minute)
        state, events = cycle(row(next_stamp, price=9.2, plan_day="2026-10-09"),
                              state, next_stamp)
        assert not events
    next_stamp += timedelta(minutes=20)
    _, events = cycle(row(next_stamp, price=8.9, plan_day="2026-10-09"),
                      state, next_stamp)
    assert len(events) == 1 and new_epoch in events[0]["idempotency_key"]


def test_price_and_position_alone_do_not_prove_executable_exit():
    now = datetime(2026, 10, 9, 10, 5, tzinfo=TZ)
    state, _ = cycle(row(now, price=8.8), {}, now)
    for minute in (20, 40):
        stamp = now + timedelta(minutes=minute)
        state, _ = cycle(row(stamp, price=9.2), state, stamp)
    stamp += timedelta(minutes=20)
    blocked = row(stamp, price=8.9, tradeable=False)
    blocked["sellable_quantity"] = None
    _, events = cycle(blocked, state, stamp)
    assert not events


def test_expired_plan_or_mismatched_quote_basis_fails_closed():
    now = datetime(2026, 10, 9, 10, 5, tzinfo=TZ)
    stale = row(now, price=8.8)
    stale["plan_valid_until"] = (now - timedelta(minutes=1)).isoformat()
    policy, events = monitor._critical_stop_policy(
        [stale], {}, now,
        {"run_id": "formal-run-1", "metadata": {"market_as_of": "2026-10-08"}})
    assert not events and policy["silent_reasons"]["plan_or_price_basis_unverified"] == 1
    stale["plan_valid_until"] = (now + timedelta(hours=6)).isoformat()
    stale["quote_adjustment"] = "raw"
    policy, events = monitor._critical_stop_policy(
        [stale], {}, now,
        {"run_id": "formal-run-1", "metadata": {"market_as_of": "2026-10-08"}})
    assert not events and policy["silent_reasons"]["plan_or_price_basis_unverified"] == 1


def test_router_is_silent_by_default_and_archive_failure_never_sends(monkeypatch):
    calls = []
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda title, body: (
        calls.append(body) or (True, "HTTP 200")))
    silent = notification_router.send("report", "常规摘要", "无最高级事件",
                                      source="jobs.jobs_hub", only_channels=["qq"])
    assert not silent and silent.policy_status == "silent_critical_only"
    event = {"policy_version": monitor.CRITICAL_POLICY_VERSION, "level": "critical",
             "trigger_type": "hard_risk_stop", "final_action": "sell",
             "symbol": "600001", "epoch": "a" * 12, "episode": 1}
    monkeypatch.setattr(notification_router, "archive_action", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    result = notification_router.send(
        "alert", "最高级止损风险", "审计样本", source="jobs.intraday_decision_monitor",
        idempotency_key="critical-stop:600001:aaaaaaaaaaaa:sell:1",
        critical_event=event, only_channels=["qq"])
    assert result["qq"] == (False, "archive_unavailable") and not calls


def test_same_critical_event_uses_one_channel_and_one_archived_attempt(monkeypatch):
    calls = []
    seen = set()
    monkeypatch.setitem(notification_router.CHANNELS, "qq", lambda *_: (
        calls.append("qq") or (True, "HTTP 200")))
    monkeypatch.setitem(notification_router.CHANNELS, "email", lambda *_: (
        calls.append("email") or (True, "HTTP 200")))

    def archive(action, payload):
        if action == "prepare":
            key = payload["idempotency_key"]
            send = key not in seen
            seen.add(key)
            return {"message_id": "a" * 32, "delivery_id": "b" * 32,
                    "should_send": send}
        return {"started": True} if action == "start" else {"recorded": True}

    monkeypatch.setattr(notification_router, "archive_action", archive)
    event = {"policy_version": monitor.CRITICAL_POLICY_VERSION, "level": "critical",
             "trigger_type": "hard_risk_stop", "final_action": "sell",
             "symbol": "600001", "epoch": "a" * 12, "episode": 1}
    kwargs = {"source": "jobs.intraday_decision_monitor",
              "idempotency_key": "critical-stop:600001:aaaaaaaaaaaa:sell:1",
              "critical_event": event, "only_channels": ["qq", "email"]}
    first = notification_router.send("alert", "最高级止损风险", "示例", **kwargs)
    repeat = notification_router.send(
        "alert", "最高级止损风险", "示例",
        **{**kwargs, "only_channels": ["email"]})
    assert first["qq"][0] is True
    assert repeat["qq"] == (False, "archive_suppressed")
    assert calls == ["qq"]


def test_legacy_direct_portfolio_delivery_is_silent_without_policy():
    calls = []
    result = archived_call(
        channel="email", title="日常分析", original_body="摘要", final_body="摘要",
        sender=lambda: calls.append("sent") or True,
        source="notify.notification_service.portfolio", category="report")
    assert result is False and calls == []
