"""Append-only fund ledger. Decimal facts, replayed FIFO lots, atomic idempotent imports."""

from __future__ import annotations
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
import json
from fund.fund_contracts import code6, day, decimal, fingerprint, now

BUY = {"申购", "买入", "定投"}
SELL = {"赎回", "卖出"}
KINDS = BUY | SELL | {"现金分红", "红利再投", "拆分", "收益结转", "冲正"}
D = Decimal


def _db():
    import fund_db

    return fund_db


def create_schema(cur):
    from pathlib import Path

    # Migration and runtime share the same schema source.
    text = (
        Path(__file__).resolve().parents[1]
        / "scripts/migrations/22-fund-research-upgrade.sql"
    ).read_text()
    for statement in text.split(";"):
        if statement.strip() and not statement.strip().startswith(
            "INSERT INTO research_schema_migrations"
        ):
            cur.execute(statement)


@contextmanager
def transaction(write=False):
    db = _db()
    conn = db._conn()
    try:
        cur = conn.cursor()
        if write:
            if db.is_postgres():
                # One fund-ledger lock protects cross-code batches and preview version checks.
                cur.execute("SELECT pg_advisory_xact_lock(73490126)")
            else:
                cur.execute("BEGIN IMMEDIATE")
        yield cur
        if write:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def normalize(row, index=0):
    aliases = {
        "代码": "code",
        "类型": "txn_type",
        "净值": "nav",
        "金额": "amount",
        "份额": "shares",
        "费用": "fee",
        "日期": "trade_date",
        "名称": "name",
        "备注": "note",
        "date": "trade_date",
        "type": "txn_type",
    }
    r = {aliases.get(k, k): v for k, v in row.items()}
    code = code6(r.get("code", ""))
    kind = r.get("txn_type") or "申购"
    if kind not in KINDS:
        raise ValueError("未知流水类型")
    td = day(r.get("confirmation_date") or r.get("trade_date") or now()[:10])
    if td > now()[:10]:
        raise ValueError("未来成交不能记为已确认")
    account = str(r.get("account") or "default").strip()
    channel = str(r.get("channel") or "default").strip()
    if not account or not channel:
        raise ValueError("账户和渠道不能为空")
    nav = decimal(r.get("nav") or 0, "0.000001")
    fee = decimal(r.get("fee") or 0)
    if fee < 0:
        raise ValueError("费用不能为负")
    amount = decimal(r["amount"]) if r.get("amount") is not None else None
    shares = decimal(r["shares"], "0.0001") if r.get("shares") is not None else None
    if amount is not None and amount < 0:
        raise ValueError("金额不能为负")
    if shares is not None and shares < 0:
        raise ValueError("份额不能为负")
    if kind in BUY:
        if nav <= 0:
            raise ValueError("成交净值必须大于零")
        if shares is None:
            if amount is None or amount <= fee:
                raise ValueError("申购金额须大于费用")
            shares = decimal((amount - fee) / nav, "0.0001")
        if shares <= 0:
            raise ValueError("份额必须大于零")
        expected = decimal(shares * nav + fee)
        if amount is None:
            amount = expected
        if abs(amount - expected) > D(".01"):
            raise ValueError("金额、份额、净值及费用不一致")
    elif kind in SELL:
        if nav <= 0:
            raise ValueError("成交净值必须大于零")
        if shares is None:
            if amount is None:
                raise ValueError("赎回必须提供份额或到账金额")
            shares = decimal((amount + fee) / nav, "0.0001")
        if shares <= 0:
            raise ValueError("份额必须大于零")
        expected = decimal(shares * nav - fee)
        if amount is None:
            amount = expected
        if amount < 0 or abs(amount - expected) > D(".01"):
            raise ValueError("赎回金额不一致")
    elif kind in {"红利再投", "收益结转"}:
        if shares is None or shares <= 0:
            raise ValueError("再投/结转须提供确认份额")
        if nav <= 0:
            raise ValueError("再投/结转须提供确认净值")
        amount = amount or decimal(shares * nav)
    elif kind == "现金分红":
        if amount is None or amount <= 0:
            raise ValueError("分红须提供金额")
        shares = D(0)
    elif kind == "拆分":
        ratio = decimal(r.get("ratio", 0), "0.00000001")
        if ratio <= 0:
            raise ValueError("拆分比例必须大于零")
        shares = D(0)
        amount = D(0)
    else:
        if not r.get("reverses"):
            raise ValueError("冲正须引用原事件 ID")
        shares = D(0)
        amount = D(0)
    result = {
        "code": code,
        "account": account,
        "channel": channel,
        "txn_type": kind,
        "nav": str(nav),
        "shares": str(shares),
        "amount": str(amount),
        "fee": str(fee),
        "confirmation_date": td,
        "trade_date": td,
        "application_date": day(r.get("application_date") or r.get("nav_date") or td),
        "nav_date": day(r.get("nav_date") or td),
        "settlement_date": day(r.get("settlement_date") or td),
        "name": r.get("name"),
        "note": r.get("note"),
        "source": r.get("source") or "manual",
        "reverses": r.get("reverses"),
        "ratio": str(r.get("ratio") or 1),
        "occurrence_id": r.get("occurrence_id"),
    }
    if result["application_date"] > result["nav_date"] or result["nav_date"] > td:
        raise ValueError("申请/净值/确认日期顺序不一致")
    earliest = result["application_date"] if kind in BUY else td
    if result["settlement_date"] < earliest:
        raise ValueError("资金到账/扣款日期不符合交易类型")
    external = r.get("external_id")
    economic = {k: v for k, v in result.items() if k not in ("name", "note", "source")}
    identity = (
        {"account": account, "channel": channel, "external_id": str(external)}
        if external
        else economic
    )
    result["event_id"] = fingerprint(identity)
    result["external_id"] = external
    return result


def _load(cur, codes):
    openings, events = {}, []
    for code in sorted(set(codes)):
        cur.execute("SELECT payload FROM fund_ledger_openings WHERE code=?", (code,))
        row = cur.fetchone()
        if row:
            openings[code] = json.loads(row[0])
        else:
            cur.execute(
                "SELECT name,shares,cost_nav FROM fund_holdings WHERE code=?", (code,)
            )
            old = cur.fetchone()
            openings[code] = {
                "code": code,
                "name": old[0] if old else None,
                "shares": str(old[1] or 0) if old else "0",
                "cost_nav": str(old[2] or 0) if old else "0",
                "as_of": now()[:10] if old and old[1] else None,
                "account": "default",
                "channel": "default",
                "history_status": "opening_only"
                if old and old[1]
                else "complete_from_zero",
            }
        cur.execute(
            "SELECT payload FROM fund_ledger_events WHERE code=? ORDER BY event_date,event_id",
            (code,),
        )
        events.extend(json.loads(r[0]) for r in cur.fetchall())
    return openings, events


def replay(openings, events):
    positions, lots, history, cash = {}, {}, [], {}
    byid = {e["event_id"]: e for e in events}
    reversed_ids = set()
    for event in events:
        if event["txn_type"] != "冲正":
            continue
        target = byid.get(event["reverses"])
        if (
            not target
            or target["txn_type"] == "冲正"
            or target["account"] != event["account"]
            or target["code"] != event["code"]
            or target["channel"] != event["channel"]
        ):
            raise ValueError("冲正引用无效")
        if target["confirmation_date"] > event["confirmation_date"]:
            raise ValueError("冲正日期不能早于原成交")
        if event["reverses"] in reversed_ids:
            raise ValueError("同一事件不能重复冲正")
        reversed_ids.add(event["reverses"])
    for code, opening in openings.items():
        if D(opening["shares"]) < 0 or D(opening["cost_nav"]) < 0:
            raise ValueError("期初份额和成本不能为负")
        if opening.get("as_of") and opening["as_of"] > now()[:10]:
            raise ValueError("期初日期不能在未来")
        key = (opening["account"], opening["channel"], code)
        sh, cost = D(opening["shares"]), D(opening["cost_nav"])
        positions[key] = {"shares": sh, "cost": sh * cost, "name": opening.get("name")}
        lots[key] = (
            [
                {
                    "date": opening.get("acquired_date"),
                    "shares": sh,
                    "cost": sh * cost,
                    "opening": True,
                }
            ]
            if sh
            else []
        )
    # Same-date caller sequence is frozen on insertion, never sorted by random hashes.
    ordered = sorted(
        events,
        key=lambda e: (e["confirmation_date"], e.get("sequence", 0), e["event_id"]),
    )
    for e in ordered:
        if e["event_id"] in reversed_ids or e["txn_type"] == "冲正":
            continue
        code, kind = e["code"], e["txn_type"]
        opening = openings[code]
        if opening.get("as_of") and e["confirmation_date"] < opening["as_of"]:
            raise ValueError("流水早于期初余额，须先提供可核验的历史期初余额迁移预览")
        key = (e["account"], e["channel"], code)
        pos = positions.setdefault(
            key, {"shares": D(0), "cost": D(0), "name": e.get("name")}
        )
        if e.get("name"):
            pos["name"] = e["name"]
        bucket = lots.setdefault(key, [])
        sh, amt, fee = D(e["shares"]), D(e["amount"]), D(e["fee"])
        paid = received = delta = D(0)
        if kind in BUY:
            pos["shares"] += sh
            pos["cost"] += amt
            paid = amt
            delta = sh
            bucket.append(
                {
                    "date": e["confirmation_date"],
                    "shares": sh,
                    "cost": amt,
                    "event_id": e["event_id"],
                }
            )
        elif kind in SELL:
            if sh > pos["shares"]:
                raise ValueError("赎回份额超过该账户渠道可用份额")
            left, removed = sh, D(0)
            for lot in bucket:
                take = min(left, lot["shares"])
                if take:
                    cost = lot["cost"] * take / lot["shares"]
                    removed += cost
                    lot["cost"] -= cost
                    lot["shares"] -= take
                    left -= take
            pos["shares"] -= sh
            pos["cost"] -= removed
            received = amt
            delta = -sh
        elif kind in {"红利再投", "收益结转"}:
            pos["shares"] += sh
            delta = sh
            bucket.append(
                {
                    "date": e["confirmation_date"],
                    "shares": sh,
                    "cost": D(0),
                    "event_id": e["event_id"],
                }
            )
        elif kind == "拆分":
            ratio = D(e["ratio"])
            delta = pos["shares"] * (ratio - 1)
            pos["shares"] *= ratio
            for lot in bucket:
                lot["shares"] *= ratio
        elif kind == "现金分红":
            received = amt
        history.append(
            {
                **e,
                "shares": float(sh),
                "amount": float(amt),
                "fee": float(fee),
                "nav": float(e["nav"]),
                "delta_shares": float(delta),
                "cash_paid": float(paid),
                "cash_received": float(received),
                "pos_shares": float(pos["shares"]),
                "pos_cost_nav": float(pos["cost"] / pos["shares"])
                if pos["shares"]
                else 0.0,
                "settled": e["settlement_date"] <= now()[:10],
            }
        )
        cash[key] = cash.get(key, D(0)) - paid + received
    rows = []
    for (account, channel, code), p in positions.items():
        rows.append(
            {
                "code": code,
                "account": account,
                "channel": channel,
                "name": p["name"],
                "shares": float(p["shares"]),
                "cost": float(p["cost"]),
                "shares_decimal": str(p["shares"]),
                "cost_decimal": str(p["cost"]),
                "cost_nav": float(p["cost"] / p["shares"]) if p["shares"] else 0.0,
                "lots": [
                    {**lot, "shares": str(lot["shares"]), "cost": str(lot["cost"])}
                    for lot in lots[(account, channel, code)]
                    if lot["shares"]
                ],
            }
        )
    return {
        "positions": rows,
        "transactions": history,
        "reversed_ids": sorted(reversed_ids),
        "history": list(openings.values()),
    }


def _preview(cur, rows, expected=None):
    incoming = [normalize(r, i) for i, r in enumerate(rows)]
    codes = [r["code"] for r in incoming]
    openings, previous = _load(cur, codes)
    version = fingerprint({"openings": openings, "events": previous})
    if expected and expected != version:
        raise ValueError("账本已变化，请重新预览")
    existing = {e["event_id"]: e for e in previous}
    added, skipped = [], 0
    next_sequence = max((e.get("sequence", 0) for e in previous), default=0)
    for event in incoming:
        old = existing.get(event["event_id"])
        if old:
            comparable = lambda x: {
                k: v
                for k, v in x.items()
                if k not in ("recorded_at", "sequence", "name", "note", "source")
            }
            if comparable(old) != comparable(event):
                raise ValueError("相同外部交易 ID 的内容发生冲突")
            skipped += 1
            continue
        next_sequence += 1
        event.update(sequence=next_sequence, recorded_at=now())
        added.append(event)
        existing[event["event_id"]] = event
    result = replay(openings, previous + added)
    return {
        "version": version,
        "imported": len(added),
        "skipped": skipped,
        "errors": [],
        **result,
        "events": added,
        "openings": openings,
    }


def preview(rows):
    with transaction() as cur:
        return _preview(cur, rows)


def commit(rows, expected=None):
    with transaction(True) as cur:
        result = _preview(cur, rows, expected)
        for code, opening in result["openings"].items():
            cur.execute(
                "INSERT INTO fund_ledger_openings(code,payload) VALUES (?,?) ON CONFLICT(code) DO NOTHING",
                (code, json.dumps(opening, ensure_ascii=False)),
            )
        for event in result["events"]:
            cur.execute(
                "INSERT INTO fund_ledger_events(event_id,account,code,event_date,payload) VALUES (?,?,?,?,?)",
                (
                    event["event_id"],
                    event["account"],
                    event["code"],
                    event["confirmation_date"],
                    json.dumps(event, ensure_ascii=False),
                ),
            )
        affected = {
            e.get("occurrence_id")
            for e in result["transactions"]
            if e.get("occurrence_id")
        }
        affected.update(
            e.get("occurrence_id") for e in result["events"] if e.get("occurrence_id")
        )
        cur.execute(
            "SELECT item_key,payload FROM fund_workspace WHERE kind='occurrence'"
        )
        for key, payload in cur.fetchall():
            item = json.loads(payload)
            prior_ids = set(item.get("event_ids", []))
            if key not in affected and not prior_ids.intersection(
                result["reversed_ids"]
            ):
                continue
            matches = [
                e for e in result["transactions"] if e.get("occurrence_id") == key
            ]
            for event in matches:
                if event["confirmation_date"] < item["execution_date"]:
                    raise ValueError("成交确认早于计划执行日")
                if (
                    item["code"] != event["code"]
                    or item.get("account", "default") != event["account"]
                    or item.get("channel", "default") != event["channel"]
                    or event["txn_type"] not in BUY
                ):
                    raise ValueError("成交与计划账户/基金/类型不匹配")
                if item["status"] in ("cancelled", "failed"):
                    raise ValueError("已撤销/失败事项不能确认成交")
            confirmed = sum(D(str(e["amount"])) for e in matches)
            if confirmed > D(str(item["amount"])) + D(".01"):
                raise ValueError("累计成交金额超过计划金额")
            item.update(
                status="confirmed"
                if confirmed >= D(str(item["amount"])) - D(".01")
                else "partial"
                if confirmed
                else "pending",
                confirmed_amount=str(confirmed),
                event_ids=[e["event_id"] for e in matches],
            )
            _save(cur, "occurrence", key, item)
        # A bogus occurrence reference must roll back both events and holdings.
        for event in result["events"]:
            if event.get("occurrence_id"):
                cur.execute(
                    "SELECT 1 FROM fund_workspace WHERE kind='occurrence' AND item_key=?",
                    (event["occurrence_id"],),
                )
                if not cur.fetchone():
                    raise ValueError("定投事项不存在")
        project(cur, result["positions"], result["openings"])
        return {k: v for k, v in result.items() if k not in ("openings", "events")}


def project(cur, positions, codes):
    for code in codes:
        items = [p for p in positions if p["code"] == code]
        sh = sum(D(p["shares_decimal"]) for p in items)
        cost = sum(D(p["cost_decimal"]) for p in items)
        name = next((p["name"] for p in items if p["name"]), None)
        cur.execute(
            """INSERT INTO fund_holdings(code,name,shares,cost_nav) VALUES (?,?,?,?)
          ON CONFLICT(code) DO UPDATE SET shares=excluded.shares,cost_nav=excluded.cost_nav,
          name=COALESCE(excluded.name,fund_holdings.name)""",
            (code, name, float(sh), float(cost / sh) if sh else 0),
        )


def read(codes=None):
    with transaction() as cur:
        if codes is None:
            cur.execute("SELECT code FROM fund_ledger_openings")
            codes = [r[0] for r in cur.fetchall()]
        openings, events = _load(cur, codes)
        return replay(openings, events)


def _save(cur, kind, key, payload):
    cur.execute(
        """INSERT INTO fund_workspace(kind,item_key,payload) VALUES (?,?,?)
                ON CONFLICT(kind,item_key) DO UPDATE SET payload=excluded.payload""",
        (
            kind,
            key,
            json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False),
        ),
    )


def save(kind, key, payload):
    with transaction(True) as cur:
        _save(cur, kind, key, payload)
    return payload


def list_saved(kind):
    with transaction() as cur:
        cur.execute(
            "SELECT payload FROM fund_workspace WHERE kind=? ORDER BY item_key", (kind,)
        )
        return [json.loads(r[0]) for r in cur.fetchall()]


def migration_preview(openings):
    with transaction() as cur:
        codes = [code6(x["code"]) for x in openings]
        current, events = _load(cur, codes)
        target = {
            code6(x["code"]): {
                **x,
                "code": code6(x["code"]),
                "account": x.get("account", "default"),
                "channel": x.get("channel", "default"),
                "as_of": day(x["as_of"]),
                "shares": str(decimal(x["shares"], "0.0001")),
                "cost_nav": str(decimal(x["cost_nav"], "0.000001")),
                "history_status": "user_verified_opening",
            }
            for x in openings
        }
        result = replay(target, events)
        return {
            "before": current,
            "after": target,
            "result": result,
            "version": fingerprint({"openings": current, "events": events}),
            "warnings": ["历史 dca_auto 记录需逐笔核验，未自动删除或认定为真实成交"],
        }


def migrate(openings, expected):
    # Recheck and write under the same lock; legacy events are retained separately.
    with transaction(True) as cur:
        codes = [code6(x["code"]) for x in openings]
        current, events = _load(cur, codes)
        if fingerprint({"openings": current, "events": events}) != expected:
            raise ValueError("账本已变化，请重新预览")
        target = {
            code6(x["code"]): {
                **x,
                "code": code6(x["code"]),
                "account": x.get("account", "default"),
                "channel": x.get("channel", "default"),
                "as_of": day(x["as_of"]),
                "shares": str(decimal(x["shares"], "0.0001")),
                "cost_nav": str(decimal(x["cost_nav"], "0.000001")),
                "history_status": "user_verified_opening",
            }
            for x in openings
        }
        result = replay(target, events)
        for code, item in target.items():
            cur.execute(
                "INSERT INTO fund_ledger_openings(code,payload) VALUES (?,?) ON CONFLICT(code) DO UPDATE SET payload=excluded.payload",
                (code, json.dumps(item, ensure_ascii=False)),
            )
        project(cur, result["positions"], target)
        _save(
            cur,
            "migration",
            fingerprint(target),
            {"before": current, "after": target, "recorded_at": now()},
        )
        return result
