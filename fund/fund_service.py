"""Fund use cases shared by HTTP, MCP and scheduled jobs."""

from __future__ import annotations
from datetime import date, timedelta
import json
from fund.fund_contracts import VERSION, code6, daily_result, fingerprint, now, product
from fund import fund_ledger as ledger


def open_dates_for(plan, start, end):
    if plan.get("open_dates") is not None:
        return sorted(d for d in plan["open_dates"] if str(start) <= d <= str(end))
    import fund_data

    fund_type = fund_data.fund_type(plan["code"])
    if not fund_type:
        raise ValueError("基金类型未核实，不能确认申购日历")
    text = (fund_type + " " + (fund_data.fund_name(plan["code"]) or "")).upper()
    if any(marker in text for marker in ("QDII", "海外", "美元")):
        raise ValueError("境外基金须配置核验后的产品开放日，不能直接套用 A 股日历")
    if plan.get("calendar_id", "CN") != "CN":
        raise ValueError("产品日历未配置，不能用 A 股日历替代境外申购日历")
    from data.research_store import ResearchStore

    store = ResearchStore(ensure_schema=False)
    conn = store.connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT e.trade_date,COUNT(DISTINCT e.provider),MIN(e.is_open),MAX(e.is_open) FROM research_trade_calendar_evidence e
          WHERE e.trade_date>=? AND e.trade_date<=? AND EXISTS (
            SELECT 1 FROM research_calendar_fetch_runs f WHERE f.provider=e.provider
            AND f.quality_status='ok' AND f.range_start<=e.trade_date AND f.range_end>=e.trade_date)
          GROUP BY e.trade_date ORDER BY e.trade_date""",
            (str(start), str(end)),
        )
        rows = cur.fetchall()
        expected = (
            date.fromisoformat(str(end)) - date.fromisoformat(str(start))
        ).days + 1
        if len(rows) != expected or any(int(r[1]) < 2 or r[2] != r[3] for r in rows):
            raise ValueError("产品日历存在证据缺口或分歧")
        return [str(r[0])[:10] for r in rows if int(r[2]) == 1]
    finally:
        conn.close()


def process_due_plans(today):
    from fund.fund_calendar import executions
    import fund_db

    fund_db.init_db()
    end = (today.date() if hasattr(today, "hour") else today).isoformat()
    created = []
    for p in fund_db.get_plans(only_enabled=True):
        if not p.get("anchor_date"):
            ledger.save(
                "plan_status",
                str(p["id"]),
                {
                    "id": p["id"],
                    "status": "needs_anchor",
                    "reason": "旧计划须核验起始日",
                },
            )
            continue  # Legacy schedule needs an explicit anchor before generating financial intentions.
        start = p["anchor_date"]
        if start > end:
            continue
        try:
            opened = open_dates_for(p, start, end)
        except ValueError as exc:
            ledger.save(
                "plan_status",
                str(p["id"]),
                {"id": p["id"], "status": "pending_calendar", "reason": str(exc)},
            )
            continue
        ledger.save("plan_status", str(p["id"]), {"id": p["id"], "status": "ready"})
        for occurrence in executions(
            start, end, opened, p["period"], p["day_of"], p["anchor_date"]
        ):
            identity = {
                "plan_id": p["id"],
                "scheduled_date": occurrence["scheduled_date"],
                "rule": {
                    k: p.get(k)
                    for k in ("period", "day_of", "anchor_date", "account", "channel")
                },
            }
            key = fingerprint(identity)
            item = {
                **identity,
                **occurrence,
                "occurrence_id": key,
                "code": p["code"],
                "name": p.get("name"),
                "amount": p["amount"],
                "account": p.get("account", "default"),
                "channel": p.get("channel", "default"),
                "status": "pending",
                "created_at": now(),
                "legacy_auto_record": bool(p.get("auto_record")),
            }
            with ledger.transaction(True) as cur:
                cur.execute(
                    "SELECT 1 FROM fund_workspace WHERE kind='occurrence' AND item_key=?",
                    (key,),
                )
                if cur.fetchone():
                    continue
                ledger._save(cur, "occurrence", key, item)
            created.append(item)
    return created


def pending_plans():
    return [
        r
        for r in ledger.list_saved("occurrence")
        if r["status"] in ("pending", "partial")
    ]


def holdings_snapshot(as_of=None, *, holdings=None, navs=None, transactions=None):
    import fund_db

    as_of = as_of or now()[:10]
    holdings = fund_db.get_holdings() if holdings is None else holdings
    navs = (
        fund_db.get_nav_pairs([h["code"] for h in holdings], upto_date=as_of)
        if navs is None
        else navs
    )
    books = None
    if transactions is None:
        books = ledger.read()
        transactions = books["transactions"]
    rows = []
    profiles = {r["code"]: r for r in ledger.list_saved("profile")} if holdings else {}
    for h in holdings:
        code = code6(h["code"])
        info = navs.get(code, {})
        tx = [
            t
            for t in transactions
            if t["code"] == code and t["confirmation_date"] == as_of
        ]
        if float(h.get("shares") or 0) <= 0 and not tx:
            continue
        profile = profiles.get(code, product(code, h.get("name")))
        daily = daily_result(
            {**h, "expected_nav_date": profile.get("expected_nav_date")},
            info,
            as_of,
            tx,
        )
        cost = float(h.get("shares") or 0) * float(h.get("cost_nav") or 0)
        mv = daily["mv"]
        rows.append(
            {
                **h,
                **daily,
                "nav": info.get("unit_nav"),
                "est_nav": info.get("unit_nav"),
                "daily_return": info.get("daily_return")
                if daily["state"] == "ready"
                else None,
                "cost": round(cost, 2),
                "pnl": round(mv - cost, 2) if mv is not None else None,
                "pnl_pct": mv / cost - 1 if cost and mv is not None else None,
                "profile": profile,
                "data_basis": info.get("unit_nav_kind") or "confirmed_nav",
                "source": info.get("source"),
                "unknown_cost_history": not books
                or any(
                    x["code"] == code and x["history_status"] != "complete_from_zero"
                    for x in books["history"]
                ),
                "lots": [
                    lot
                    for pos in (books or {}).get("positions", [])
                    if pos["code"] == code
                    for lot in pos["lots"]
                ],
            }
        )
    fresh = sum(r["today_pnl"] is not None for r in rows)
    summary = {
        "schema_version": VERSION,
        "as_of": as_of,
        "holdings": rows,
        "total_count": len(rows),
        "fresh_count": fresh,
        "coverage": fresh / len(rows) if rows else 1.0,
        "total_mv": round(sum(r["mv"] for r in rows if r["mv"] is not None), 2),
        "valuation_count": sum(r["mv"] is not None for r in rows),
        "confirmed_daily_pnl": round(
            sum(r["today_pnl"] for r in rows if r["today_pnl"] is not None), 2
        )
        if fresh
        else None,
        "status": "ready" if fresh == len(rows) else "partial",
        "pending": pending_plans() if holdings is not None else [],
    }
    if books is not None:
        summary["investor_performance"] = investor_performance(summary, books)
    return summary


def run_experiment(code, **config):
    import fund_data, fund_dca
    from application.results import clean_json

    profile = next(
        (p for p in ledger.list_saved("profile") if p["code"] == code6(code)), None
    )
    profile = profile or product(
        code, fund_data.fund_name(code), fund_data.fund_type(code)
    )
    if profile.get("execution", "off_exchange") != "off_exchange":
        return record_experiment(
            code,
            config,
            None,
            {
                "status": "unsupported",
                "error": "场内产品需要成交价格、交易费用与溢折价模型，不能以净值回测代替",
            },
        )
    frame = fund_data.get_nav_history(code, config.get("start"), config.get("end"))
    if frame is None:
        return record_experiment(
            code, config, None, {"status": "error", "error": "净值不可用"}
        )
    frame.attrs["code"] = code6(code)
    runtime = dict(config)
    if runtime.get("valuation_series") is not None:
        import pandas as pd

        runtime["valuation_series"] = pd.DataFrame(runtime["valuation_series"])
    result = fund_dca.dca_backtest(frame, **runtime)
    return record_experiment(code, config, frame, result)


def record_experiment(code, config, frame, result):
    from fund.fund_contracts import nav_records
    from application.results import clean_json

    records = nav_records(frame) if frame is not None and not frame.empty else []
    metadata = frame.attrs if frame is not None else {}
    result = dict(result)
    from fund.fund_contracts import engine_hash

    result.setdefault("engine_hash", engine_hash())
    result.setdefault(
        "experiment_id",
        fingerprint(
            {
                "code": code,
                "config": config,
                "input": records,
                "status": result.get("status"),
            }
        ),
    )
    ledger.save(
        "experiment",
        result["experiment_id"],
        clean_json(
            {
                "code": code6(code),
                "config": config,
                "input": records,
                "input_metadata": metadata,
                "result": result,
                "created_at": now(),
            }
        ),
    )
    return result


def replay_experiment(experiment_id):
    import pandas as pd
    import fund_dca

    item = next(
        (
            r
            for r in ledger.list_saved("experiment")
            if r["result"]["experiment_id"] == experiment_id
        ),
        None,
    )
    if not item:
        raise ValueError("实验不存在")
    from fund.fund_contracts import engine_hash

    if item["result"].get("engine_hash") != engine_hash():
        return {
            "status": "unsupported",
            "error": "计算规则版本已变化，原结果保留；请以当前规则新建实验",
            "experiment_id": experiment_id,
            "stored_engine_hash": item["result"].get("engine_hash"),
        }
    if not item["input"]:
        return {**item["result"], "replay_status": "no_input_recorded"}
    f = pd.DataFrame(item["input"])
    f["date"] = pd.to_datetime(f["date"])
    f.attrs.update(item["input_metadata"])
    config = dict(item["config"])
    if config.get("valuation_series") is not None:
        config["valuation_series"] = pd.DataFrame(config["valuation_series"])
    result = fund_dca.dca_backtest(f, **config)
    result.setdefault("experiment_id", experiment_id)
    return result


def refresh_profile(code):
    import fund_data
    from application.results import clean_json

    code = code6(code)
    existing = next(
        (r for r in ledger.list_saved("profile") if r["code"] == code), None
    )
    name, ftype = fund_data.fund_name(code), fund_data.fund_type(code)
    # Provider frames contain pandas.NA/NaN. Normalize before truth tests and
    # comparisons; cleaning only the final profile is too late.
    observed_basic = clean_json(fund_data.get_fund_basic(code))
    basic = observed_basic or (existing or {}).get("basic", {})
    top = fund_data.top_holdings(code)
    profile = product(
        code,
        name,
        ftype,
        manager=basic.get("基金经理") or basic.get("基金经理姓名"),
        basic=basic,
        top_holdings=top,
        source="eastmoney/xueqiu",
        retrieved_at=now(),
        published_at=None,
    )
    if existing:
        profile["changes"] = [
            {
                "field": k,
                "before": existing.get("basic", {}).get(k),
                "after": v,
                "first_seen_at": now(),
            }
            for k, v in basic.items()
            if existing.get("basic", {}).get(k) != v
        ]
    if existing:
        for field, source in existing.get("field_sources", {}).items():
            if source.get("source") == "user_confirmed":
                profile[field] = existing.get(field)
        profile["field_sources"] = existing.get("field_sources", {})
    profile["fetch_status"] = "ready" if observed_basic else "partial"
    if not observed_basic and existing:
        profile["retrieved_at"] = existing.get("retrieved_at")
    profile = clean_json(profile)
    ledger.save("profile_revision", fingerprint(profile), profile)
    ledger.save("profile", code, profile)
    return profile


def research_context(code, refresh=False):
    import fund_data, fund_analysis
    from application.research_artifacts import build_research_artifact
    from application.results import clean_json

    code = code6(code)
    profile = next((r for r in ledger.list_saved("profile") if r["code"] == code), None)
    if refresh or profile is None:
        profile = refresh_profile(code)
    frame = fund_data.get_nav_history(code)
    card = score(code, frame=frame, profile=profile)
    facts = {"profile": profile, "performance": card}
    decision_at = now()
    artifact = build_research_artifact(
        subject="fund:" + code,
        run_id=fingerprint(facts),
        facts=facts,
        provenance={
            "decision_at": decision_at,
            "market_as_of": card.get("metrics", {}).get("end"),
            "schema_version": VERSION,
        },
        data_quality={"status": card.get("status", "partial")},
        formal=False,
        artifact_kind="fund-research",
        analysis={
            "reasons": [card.get("advice", "")],
            "risks": ["披露持仓有时差，不能视为当前全量持仓"],
            "invalidation_conditions": [
                "经理、基准、费率、交易限制或新披露事实发生变化"
            ],
            "next_actions": ["复核新披露资料与账户资金约束"],
        },
        created_at=decision_at,
    )
    ledger.save("research", artifact["artifact_id"], clean_json(artifact))
    return {
        "code": code,
        "profile": profile,
        "score": card,
        "artifact": artifact,
        "data_basis": "observed_now_not_historical_pit",
    }


def render_due(items):
    """Same payload for archive, delivery and tests; no hidden truncated test stub."""
    if not items:
        return ""
    return (
        "🏦 定投待确认\n"
        + "\n".join(
            f"· {r['code']} {r.get('name') or ''} {float(r['amount']):,.2f}元 · 到期{r['scheduled_date']} · 待成交确认"
            for r in items
        )
        + "\n确认实际成交净值、份额和费用后更新持仓。"
    )


def configure_profile(code, fields):
    code = code6(code)
    allowed = {
        "family_id",
        "currency",
        "region",
        "benchmark",
        "asset",
        "manager",
        "execution",
        "subscription_status",
        "redemption_status",
        "category",
        "expected_nav_date",
        "calendar_id",
        "credit_exposure",
        "duration",
        "equity_ratio",
        "industry_weights",
        "fee_rules",
        "holding_period",
        "benchmark_kind",
        "benchmark_currency",
    }
    if set(fields) - allowed:
        raise ValueError("包含不支持的产品配置字段")
    profile = next(
        (r for r in ledger.list_saved("profile") if r["code"] == code), product(code)
    )
    sources = profile.get("field_sources", {})
    sources.update({k: {"source": "user_confirmed", "known_at": now()} for k in fields})
    profile.update(fields, field_sources=sources)
    ledger.save("profile_revision", fingerprint(profile), profile)
    return ledger.save("profile", code, profile)


def rebalance(targets, available_cash, fee_rules=None, allow_redemptions=False):
    from fund.fund_portfolio import rebalance_preview

    snapshot = holdings_snapshot()
    profiles = {p["code"]: p for p in ledger.list_saved("profile")}
    for code in targets:
        code = code6(code)
        if not any(h["code"] == code for h in snapshot["holdings"]):
            snapshot["holdings"].append(
                {
                    "code": code,
                    "shares": 0,
                    "mv": 0,
                    "profile": profiles.get(code, {}),
                    "nav_date": snapshot["as_of"],
                }
            )
    return rebalance_preview(
        snapshot,
        targets,
        available_cash,
        fee_rules=fee_rules,
        allow_redemptions=allow_redemptions,
    )


def set_occurrence_status(key, status):
    if status not in ("cancelled", "failed", "pending"):
        raise ValueError("确认状态须通过成交回执生成")
    with ledger.transaction(True) as cur:
        cur.execute(
            "SELECT payload FROM fund_workspace WHERE kind='occurrence' AND item_key=?",
            (key,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("定投事项不存在")
        item = json.loads(row[0])
        if item["status"] in ("confirmed", "partial"):
            raise ValueError("已有成交的事项须通过冲正处理")
        item.update(status=status, updated_at=now())
        ledger._save(cur, "occurrence", key, item)
        return item


def investor_performance(snapshot, books):
    from fund_dca import xirr
    import pandas as pd

    if any(h.get("history_status") != "complete_from_zero" for h in books["history"]):
        return {
            "status": "partial",
            "reason": "期初持仓以前的现金流未知，不能计算完整持有收益",
        }
    if snapshot["valuation_count"] != snapshot["total_count"]:
        return {"status": "partial", "reason": "存在缺失估值"}
    flows = []
    for event in books["transactions"]:
        if event["confirmation_date"] > snapshot["as_of"]:
            continue
        dt = pd.Timestamp(event["settlement_date"])
        amount = event["cash_received"] - event["cash_paid"]
        flows.append((dt, amount))
    if not flows:
        return {"status": "partial", "reason": "缺少已核验现金流"}
    # Money received after valuation remains a receivable, never disappears from final wealth.
    terminal = snapshot["total_mv"]
    available = [(d, a) for d, a in flows if str(d.date()) <= snapshot["as_of"]]
    terminal += sum(a for d, a in flows if str(d.date()) > snapshot["as_of"])
    available.append((pd.Timestamp(snapshot["as_of"]), terminal))
    return {
        "status": "ready",
        "net_profit": round(sum(a for _, a in available), 2),
        "annualized_irr": xirr(available),
        "basis": "基金投资者现金流；历史从零且费用完整时可计算",
        "xirr_status": "available"
        if xirr(available) is not None
        else "short_history_or_ambiguous_root",
    }


def configure_plan(plan_id, settings):
    import fund_db
    from fund.fund_calendar import scheduled_dates

    p = next((p for p in fund_db.get_plans() if p["id"] == plan_id), None)
    if not p:
        raise ValueError("计划不存在")
    allowed = {"anchor_date", "calendar_id", "open_dates", "account", "channel"}
    if set(settings) - allowed:
        raise ValueError("已生成事项的计划规则应新建后续计划，不能重写旧周期")
    if any(r.get("plan_id") == plan_id for r in ledger.list_saved("occurrence")):
        raise ValueError("计划已有冻结事项，请停用旧计划并新建后续计划")
    merged = {**p, **settings}
    list(
        scheduled_dates(
            merged["anchor_date"],
            merged["anchor_date"],
            p["period"],
            p["day_of"],
            merged["anchor_date"],
        )
    )
    return ledger.save(
        "plan",
        str(plan_id),
        {
            k: merged[k]
            for k in (
                "id",
                "anchor_date",
                "calendar_id",
                "open_dates",
                "account",
                "channel",
            )
            if k in merged
        },
    )


def score(code, with_extras=False, frame=None, profile=None):
    import fund_analysis, fund_data

    code = code6(code)
    profile = profile or next(
        (p for p in ledger.list_saved("profile") if p["code"] == code), None
    )
    benchmark = None
    if (
        profile
        and profile.get("benchmark_kind") == "fund_total_return"
        and profile.get("benchmark_currency") == profile.get("currency")
    ):
        benchmark = fund_data.get_nav_history(code6(profile["benchmark"]))
    return fund_analysis.score_fund(
        code,
        nav_df=frame,
        with_extras=with_extras,
        profile=profile,
        benchmark_df=benchmark,
    )


def review_week(today, budget_seconds=60):
    """Budgeted once-per-code/week public-profile review. No model calls or trades."""
    import time
    import fund_db

    today = date.fromisoformat(str(today)[:10])
    week = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
    done = {r["review_key"] for r in ledger.list_saved("weekly_review")}
    deadline = time.monotonic() + budget_seconds
    results = []
    for h in fund_db.get_holdings():
        if float(h.get("shares") or 0) <= 0:
            continue
        key = week + ":" + code6(h["code"])
        if key in done:
            continue
        if time.monotonic() >= deadline:
            break
        try:
            profile = refresh_profile(h["code"])
        except Exception as exc:
            # One malformed provider response must not starve later holdings.
            result = {"review_key": key, "code": h["code"], "week": week,
                      "status": "partial", "reason": "profile_refresh_failed",
                      "error_type": type(exc).__name__, "known_at": now()}
            ledger.save("weekly_review_pending", key, result)
            results.append(result)
            continue
        status = profile.get("fetch_status", "partial")
        result = {
            "review_key": key,
            "code": h["code"],
            "week": week,
            "status": status,
            "changes": profile.get("changes", []),
            "known_at": now(),
            "source": profile.get("source"),
            "published_at": profile.get("published_at"),
        }
        if status == "ready":
            ledger.save("weekly_review", key, result)
        else:
            ledger.save("weekly_review_pending", key, result)
        results.append(result)
    return results
