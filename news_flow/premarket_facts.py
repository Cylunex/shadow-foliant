"""可追溯的 A 股盘前事实流。

这条纵向链路只负责：CLS 红讯采集、源 ID 去重、双时间窗口、结构化本地标签、
题材线程和盘前证据摘要。它不生成股票代码，不修改正式选股，也不输出买卖指令。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import _bootstrap  # noqa: F401
from db_compat import connect as db_connect


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEMA_VERSION = "premarket-facts-v1"
_FINAL_STATUSES = {"success", "success_with_warnings", "degraded", "incomplete"}


@dataclass(frozen=True)
class PremarketWindow:
    report_date: str
    previous_trade_date: str
    start: datetime
    background_end: datetime
    end: datetime

    def bucket_for(self, published_at: Any) -> str:
        stamp = _as_datetime(published_at)
        return "background" if stamp < self.background_end else "main"


class NonTradingReportDate(ValueError):
    pass


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI)


def resolve_window(report_date: str | date, trade_days: Iterable[str]) -> PremarketWindow:
    current = date.fromisoformat(str(report_date)) if not isinstance(report_date, date) else report_date
    available = sorted({date.fromisoformat(str(item)[:10]) for item in trade_days if str(item).strip()})
    if current not in available:
        raise NonTradingReportDate(f"{current.isoformat()} is not a confirmed A-share trading day")
    previous = next((item for item in reversed(available) if item < current), None)
    if previous is None:
        raise ValueError("confirmed previous A-share trading day is unavailable")
    return PremarketWindow(
        report_date=current.isoformat(),
        previous_trade_date=previous.isoformat(),
        start=datetime.combine(previous, time(8, 30), _SHANGHAI),
        background_end=datetime.combine(previous, time(15, 0), _SHANGHAI),
        end=datetime.combine(current, time(8, 30), _SHANGHAI),
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


def _clean_text(value: Any, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class PremarketFactStore:
    """PostgreSQL 事实库；表结构由 ``scripts/init_postgres.sql`` 受控迁移。"""

    def __init__(self, connection_factory: Optional[Callable[[], Any]] = None):
        self._connection_factory = connection_factory

    def connect(self):
        if self._connection_factory:
            return self._connection_factory()
        return db_connect(_bootstrap.db_path("premarket_facts.db"))

    def ingest(self, run_id: str, events: Iterable[dict], window: PremarketWindow,
               observed_at: str) -> dict[str, int]:
        inserted = revised = observed = 0
        conn = self.connect()
        try:
            cur = conn.cursor()
            for item in events:
                source = str(item.get("source") or "").strip()
                source_id = str(item.get("source_event_id") or "").strip()
                published_at = _as_datetime(item.get("published_at")).isoformat()
                if not source or not source_id or not published_at:
                    continue
                event_id = _hash("news-event", source, source_id)
                title = _clean_text(item.get("title"), 300)
                content = _clean_text(item.get("content"), 8000)
                content_hash = _hash(title, content)
                cur.execute(
                    "SELECT content_hash FROM news_events WHERE source=? AND source_event_id=?",
                    (source, source_id),
                )
                existing = cur.fetchone()
                if not existing:
                    inserted += 1
                elif str(existing[0] or "") != content_hash:
                    revised += 1
                cur.execute(
                    """INSERT INTO news_events
                       (event_id,source,source_event_id,title,content,published_at,
                        first_observed_at,last_observed_at,content_hash,level,url,
                        source_quality,raw_payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source,source_event_id) DO UPDATE SET
                        title=excluded.title,content=excluded.content,
                        published_at=excluded.published_at,last_observed_at=excluded.last_observed_at,
                        content_hash=excluded.content_hash,level=excluded.level,url=excluded.url,
                        source_quality=excluded.source_quality,raw_payload=excluded.raw_payload""",
                    (
                        event_id, source, source_id, title, content, published_at,
                        observed_at, observed_at, content_hash, str(item.get("level") or "")[:40],
                        str(item.get("url") or "")[:1000],
                        str(item.get("source_quality") or "unknown")[:40],
                        _json(item.get("raw_payload") or {}),
                    ),
                )
                revision_id = _hash(event_id, content_hash)
                cur.execute(
                    """INSERT INTO news_event_revisions
                       (revision_id,event_id,content_hash,title,content,raw_payload,first_observed_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_id,content_hash) DO NOTHING""",
                    (revision_id, event_id, content_hash, title, content,
                     _json(item.get("raw_payload") or {}), observed_at),
                )
                observation_id = _hash(run_id, event_id)
                cur.execute(
                    """INSERT INTO news_observations
                       (observation_id,run_id,event_id,observed_at,bucket,platform_rank,
                        platform_heat,url)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,event_id) DO UPDATE SET
                        observed_at=excluded.observed_at,bucket=excluded.bucket,
                        platform_rank=excluded.platform_rank,platform_heat=excluded.platform_heat,
                        url=excluded.url""",
                    (
                        observation_id, run_id, event_id, observed_at,
                        window.bucket_for(published_at), item.get("rank"), item.get("heat"),
                        str(item.get("url") or "")[:1000],
                    ),
                )
                observed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"inserted": inserted, "revised": revised, "observed": observed}

    def load_window(self, window: PremarketWindow) -> list[dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT event_id,source,source_event_id,title,content,published_at,
                          first_observed_at,last_observed_at,content_hash,level,url,source_quality
                   FROM news_events WHERE published_at>=? AND published_at<=?
                   ORDER BY published_at,event_id""",
                (window.start.isoformat(), window.end.isoformat()),
            )
            columns = [item[0] for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            conn.close()

    def load_threads(self) -> dict[str, dict]:
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT theme_id,canonical_name,status,first_seen_at,last_catalyst_at,
                          strength,confidence,evidence_event_ids
                   FROM news_theme_threads"""
            )
            columns = [item[0] for item in cur.description]
            rows = []
            for row in cur.fetchall():
                item = dict(zip(columns, row))
                item["evidence_event_ids"] = _loads(item.get("evidence_event_ids"), [])
                rows.append(item)
            return {str(item["canonical_name"]): item for item in rows}
        finally:
            conn.close()

    def save_tags(self, run_id: str, tags: Iterable[dict], tagged_at: str) -> int:
        conn = self.connect()
        count = 0
        try:
            cur = conn.cursor()
            for item in tags:
                cur.execute(
                    """INSERT INTO news_event_tags
                       (run_id,event_id,bucket,keep,importance,information_type,time_role,
                        market_relevance,topic_tags,a_share_mapping,summary,reason,tagger,
                        schema_version,evidence_verified,tagged_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,event_id) DO UPDATE SET
                        bucket=excluded.bucket,keep=excluded.keep,importance=excluded.importance,
                        information_type=excluded.information_type,time_role=excluded.time_role,
                        market_relevance=excluded.market_relevance,topic_tags=excluded.topic_tags,
                        a_share_mapping=excluded.a_share_mapping,summary=excluded.summary,
                        reason=excluded.reason,tagger=excluded.tagger,
                        schema_version=excluded.schema_version,
                        evidence_verified=excluded.evidence_verified,tagged_at=excluded.tagged_at""",
                    (
                        run_id, item["event_id"], item["bucket"], int(bool(item["keep"])),
                        int(item["importance"]), item["information_type"], item["time_role"],
                        item["market_relevance"], _json(item["topic_tags"]),
                        _json(item["a_share_mapping"]), item["summary"], item["reason"],
                        item["tagger"], _SCHEMA_VERSION, int(bool(item["evidence_verified"])),
                        tagged_at,
                    ),
                )
                count += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return count

    def save_threads(self, threads: Iterable[dict], updated_at: str) -> int:
        conn = self.connect()
        count = 0
        try:
            cur = conn.cursor()
            for item in threads:
                cur.execute(
                    """INSERT INTO news_theme_threads
                       (theme_id,canonical_name,status,first_seen_at,last_catalyst_at,
                        strength,confidence,linked_sector_ids,linked_security_ids,
                        active_assumptions,invalidation_conditions,evidence_event_ids,
                        schema_version,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(theme_id) DO UPDATE SET
                        canonical_name=excluded.canonical_name,status=excluded.status,
                        last_catalyst_at=excluded.last_catalyst_at,strength=excluded.strength,
                        confidence=excluded.confidence,
                        linked_sector_ids=excluded.linked_sector_ids,
                        linked_security_ids=excluded.linked_security_ids,
                        active_assumptions=excluded.active_assumptions,
                        invalidation_conditions=excluded.invalidation_conditions,
                        evidence_event_ids=excluded.evidence_event_ids,
                        schema_version=excluded.schema_version,updated_at=excluded.updated_at""",
                    (
                        item["theme_id"], item["canonical_name"], item["status"],
                        item["first_seen_at"], item["last_catalyst_at"], item["strength"],
                        item["confidence"], _json(item.get("linked_sector_ids") or []),
                        "[]", _json(item.get("active_assumptions") or []),
                        _json(item.get("invalidation_conditions") or []),
                        _json(item.get("evidence_event_ids") or []), _SCHEMA_VERSION, updated_at,
                    ),
                )
                count += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return count

    def save_run(self, result: dict) -> None:
        conn = self.connect()
        try:
            conn.cursor().execute(
                """INSERT INTO news_premarket_runs
                   (run_id,report_date,status,started_at,finished_at,window_start,
                    background_end,window_end,input_count,deduplicated_count,inserted_count,
                    revised_count,tagged_count,local_fallback_count,theme_count,evidence_coverage,
                    stage_status,warnings,data_notes,brief_payload,schema_version)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,finished_at=excluded.finished_at,
                    input_count=excluded.input_count,
                    deduplicated_count=excluded.deduplicated_count,
                    inserted_count=excluded.inserted_count,
                    revised_count=excluded.revised_count,
                    tagged_count=excluded.tagged_count,
                    local_fallback_count=excluded.local_fallback_count,
                    theme_count=excluded.theme_count,evidence_coverage=excluded.evidence_coverage,
                    stage_status=excluded.stage_status,warnings=excluded.warnings,
                    data_notes=excluded.data_notes,brief_payload=excluded.brief_payload""",
                (
                    result["run_id"], result["report_date"], result["status"],
                    result["started_at"], result["finished_at"], result["window"]["start"],
                    result["window"]["background_end"], result["window"]["end"],
                    result["metrics"]["input_count"], result["metrics"]["deduplicated_count"],
                    result["metrics"]["inserted_count"], result["metrics"]["revised_count"],
                    result["metrics"]["tagged_count"], result["metrics"]["local_fallback_count"],
                    result["metrics"]["theme_count"], result["metrics"]["evidence_coverage"],
                    _json(result["stage_status"]), _json(result["warnings"]),
                    _json(result["data_notes"]), _json(result["brief"]), _SCHEMA_VERSION,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def latest(self, report_date: str = "") -> dict:
        conn = self.connect()
        try:
            cur = conn.cursor()
            if report_date:
                cur.execute(
                    """SELECT run_id,report_date,status,started_at,finished_at,window_start,
                              background_end,window_end,input_count,deduplicated_count,
                              inserted_count,revised_count,tagged_count,local_fallback_count,
                              theme_count,evidence_coverage,
                              stage_status,warnings,data_notes,brief_payload,schema_version
                       FROM news_premarket_runs WHERE report_date=?
                       ORDER BY finished_at DESC LIMIT 1""",
                    (str(report_date)[:10],),
                )
            else:
                cur.execute(
                    """SELECT run_id,report_date,status,started_at,finished_at,window_start,
                              background_end,window_end,input_count,deduplicated_count,
                              inserted_count,revised_count,tagged_count,local_fallback_count,
                              theme_count,evidence_coverage,
                              stage_status,warnings,data_notes,brief_payload,schema_version
                       FROM news_premarket_runs ORDER BY report_date DESC,finished_at DESC LIMIT 1"""
                )
            row = cur.fetchone()
            if not row:
                return {"status": "unavailable", "warnings": ["暂无盘前事实流产物"]}
            columns = [item[0] for item in cur.description]
            result = dict(zip(columns, row))
            for key, fallback in (("stage_status", {}), ("warnings", []),
                                  ("data_notes", []), ("brief_payload", {})):
                result[key] = _loads(result.get(key), fallback)
            return result
        finally:
            conn.close()


_INFO_RULES = (
    ("company_announcement", ("公告", "业绩", "回购", "增持", "减持", "中标", "合同", "分红")),
    ("policy_regulation", ("国务院", "发改委", "工信部", "监管", "政策", "条例", "办法", "指导意见")),
    ("overseas_mapping", ("美联储", "美国", "欧洲", "海外", "关税", "美元", "原油", "黄金")),
    ("capital_flow", ("北向资金", "主力资金", "净流入", "净流出", "融资余额")),
    ("sector_trading", ("涨停", "跌停", "成交额", "板块上涨", "板块下跌")),
    ("market_snapshot", ("上证指数", "深证成指", "创业板指", "收盘", "开盘")),
)
_THEME_RULES = {
    "AI算力": ("人工智能", "AI", "大模型", "算力", "数据中心", "液冷"),
    "半导体": ("半导体", "芯片", "晶圆", "光刻", "存储器"),
    "机器人": ("机器人", "人形机器人", "减速器", "丝杠"),
    "新能源汽车": ("新能源汽车", "电动车", "锂电池", "智能驾驶", "汽车零部件"),
    "光伏储能": ("光伏", "储能", "逆变器", "硅料", "风电"),
    "创新药": ("创新药", "医药", "医疗器械", "临床试验"),
    "消费": ("消费", "白酒", "零售", "旅游", "家电"),
    "军工": ("军工", "国防", "航空发动机", "无人机"),
    "有色资源": ("黄金", "铜", "铝", "稀土", "有色", "锂矿"),
    "化工": ("化工", "化肥", "农药", "新材料"),
    "金融": ("银行", "证券", "保险", "金融"),
    "房地产": ("房地产", "地产", "商品房", "住房"),
    "低空经济": ("低空经济", "飞行汽车", "eVTOL"),
    "商业航天": ("商业航天", "卫星", "火箭"),
    "数据要素": ("数据要素", "数据资产", "数字经济"),
}
_RISK_WORDS = ("处罚", "调查", "终止", "亏损", "暴跌", "制裁", "违约", "退市", "减持", "下调")
_INVALIDATION_WORDS = ("证伪", "取消", "终止", "不及预期", "失败")
_IMPORTANT_WORDS = ("国务院", "央行", "证监会", "重大", "首次", "突破", "超预期", "创历史")


def _information_type(text: str) -> str:
    for name, words in _INFO_RULES:
        if any(word.lower() in text.lower() for word in words):
            return name
    return "industry_change"


def _topics(text: str, information_type: str) -> list[str]:
    matched = [name for name, words in _THEME_RULES.items()
               if any(word.lower() in text.lower() for word in words)]
    if matched:
        return matched[:3]
    if information_type == "overseas_mapping":
        return ["海外宏观"]
    if information_type in {"market_snapshot", "sector_trading", "capital_flow"}:
        return ["A股市场"]
    return ["其他产业事件"]


def tag_events(events: Iterable[dict], window: PremarketWindow,
               previous_threads: Optional[dict[str, dict]] = None) -> list[dict]:
    """低成本确定性 Tagger；每条事件独立处理，不会整批退化。"""
    previous_threads = previous_threads or {}
    tagged = []
    for event in events:
        title = _clean_text(event.get("title"), 300)
        content = _clean_text(event.get("content"), 2000)
        text = f"{title} {content}".strip()
        info_type = _information_type(text)
        topics = _topics(text, info_type)
        bucket = window.bucket_for(event.get("published_at"))
        risk = any(word in text for word in _RISK_WORDS)
        first_seen = _as_datetime(event.get("first_observed_at"))
        already_observed = first_seen < window.start
        known_topic = any(topic in previous_threads for topic in topics)
        published = _as_datetime(event.get("published_at"))
        if bucket == "background":
            time_role = "background_priced"
        elif risk:
            time_role = "risk_escalation"
        elif already_observed:
            time_role = "duplicate_update"
        elif known_topic:
            time_role = "continuation"
        elif published.date().isoformat() == window.report_date:
            time_role = "preopen_increment"
        else:
            time_role = "new_catalyst"
        relevance = "high" if info_type in {
            "policy_regulation", "company_announcement", "industry_change"
        } or topics[0] not in {"其他产业事件", "海外宏观"} else "medium"
        importance = 2
        if str(event.get("level") or "").lower() in {"a", "b", "red", "important"}:
            importance += 1
        if any(word in text for word in _IMPORTANT_WORDS):
            importance += 1
        if info_type in {"policy_regulation", "company_announcement"} or risk:
            importance += 1
        importance = min(5, importance)
        summary = title if not content or content.startswith(title) else f"{title}：{content}"
        tagged.append({
            **event,
            "bucket": bucket,
            "keep": relevance != "none" and bool(title or content),
            "importance": importance,
            "information_type": info_type,
            "time_role": time_role,
            "market_relevance": relevance,
            "topic_tags": topics,
            "a_share_mapping": topics,
            "summary": summary[:180],
            "reason": "规则标签仅归纳行业/题材，不推断股票代码",
            "tagger": "local_rules",
            "evidence_verified": bool(event.get("event_id")),
        })
    return tagged


def build_theme_threads(tags: Iterable[dict], previous_threads: Optional[dict[str, dict]],
                        observed_at: str) -> list[dict]:
    previous_threads = previous_threads or {}
    grouped: dict[str, list[dict]] = {}
    for item in tags:
        if not item.get("keep"):
            continue
        for topic in item.get("topic_tags") or ["其他产业事件"]:
            grouped.setdefault(str(topic), []).append(item)
    threads = []
    for topic, items in grouped.items():
        previous = previous_threads.get(topic)
        roles = {str(item.get("time_role")) for item in items}
        text = " ".join(str(item.get("summary") or "") for item in items)
        strength = min(100, sum(int(item.get("importance") or 0) * 10 for item in items))
        if any(word in text for word in _INVALIDATION_WORDS):
            status = "invalidated"
        elif "risk_escalation" in roles:
            status = "risk_escalation"
        elif not previous:
            status = "new"
        elif roles.intersection({"new_catalyst", "preopen_increment"}):
            status = "strengthened"
        else:
            status = "continued"
        evidence = list(dict.fromkeys(str(item["event_id"]) for item in items if item.get("event_id")))
        first_seen = str((previous or {}).get("first_seen_at") or observed_at)
        last_catalyst = max(str(item.get("published_at") or observed_at) for item in items)
        threads.append({
            "theme_id": _hash(_SCHEMA_VERSION, "theme", topic),
            "canonical_name": topic,
            "status": status,
            "first_seen_at": first_seen,
            "last_catalyst_at": last_catalyst,
            "strength": strength,
            "confidence": round(sum(int(item.get("importance") or 0) for item in items)
                                / max(5 * len(items), 1), 4),
            "linked_sector_ids": [topic] if topic not in {"海外宏观", "其他产业事件"} else [],
            "linked_security_ids": [],
            "active_assumptions": ["仅作盘前事实解释，不影响正式选股"],
            "invalidation_conditions": ["后续官方信息否定或市场价格未形成验证"],
            "evidence_event_ids": evidence,
            "event_count": len(items),
        })
    return sorted(threads, key=lambda item: (-item["strength"], item["canonical_name"]))


_ROLE_LABELS = {
    "background_priced": "昨日背景", "new_catalyst": "新催化",
    "preopen_increment": "盘前新增", "continuation": "延续",
    "duplicate_update": "重复更新", "risk_escalation": "风险升级",
}


def _brief_payload(tags: list[dict], threads: list[dict], window: PremarketWindow,
                   max_events: int = 12) -> dict:
    kept = [item for item in tags if item.get("keep")]
    kept.sort(key=lambda item: (
        item.get("bucket") != "main", -int(item.get("importance") or 0),
        str(item.get("published_at") or ""),
    ))
    facts = []
    valid_ids = {str(item.get("event_id")) for item in tags if item.get("event_id")}
    for item in kept[:max_events]:
        evidence_id = str(item.get("event_id") or "")
        facts.append({
            "event_id": evidence_id,
            "source": item.get("source"),
            "source_event_id": item.get("source_event_id"),
            "published_at": item.get("published_at"),
            "bucket": item.get("bucket"),
            "importance": item.get("importance"),
            "information_type": item.get("information_type"),
            "time_role": item.get("time_role"),
            "market_relevance": item.get("market_relevance"),
            "topic_tags": item.get("topic_tags") or [],
            "summary": item.get("summary"),
            "evidence_verified": evidence_id in valid_ids,
        })
    safe_threads = []
    for item in threads[:8]:
        evidence = [event_id for event_id in item.get("evidence_event_ids") or []
                    if event_id in valid_ids]
        if not evidence:
            continue
        safe_threads.append({key: item[key] for key in (
            "theme_id", "canonical_name", "status", "strength", "confidence", "event_count"
        )} | {"evidence_event_ids": evidence, "selection_effect": "reference_only"})
    return {
        "report_date": window.report_date,
        "previous_trade_date": window.previous_trade_date,
        "facts": facts,
        "theme_threads": safe_threads,
        "selection_effect": "reference_only",
        "security_mapping": "disabled",
    }


def render_brief(brief: dict) -> str:
    facts = brief.get("facts") or []
    if not facts:
        return "（盘前事实流暂无可用事件）"
    lines = []
    for item in facts:
        published = _as_datetime(item.get("published_at")).strftime("%m-%d %H:%M")
        role = _ROLE_LABELS.get(str(item.get("time_role")), str(item.get("time_role") or "事件"))
        topics = "/".join(item.get("topic_tags") or [])
        source_id = item.get("source_event_id") or item.get("event_id", "")[:10]
        lines.append(
            f"  [{published}][{role}][{topics}] {item.get('summary', '')} "
            f"(证据 CLS:{source_id})"
        )
    threads = brief.get("theme_threads") or []
    if threads:
        state_text = "、".join(
            f"{item['canonical_name']}={item['status']}" for item in threads[:5]
        )
        lines.append(f"  题材线程: {state_text}")
    lines.append("  边界: 事实与题材仅供解释，不生成股票代码，不改变正式 TOP15/TOP5。")
    return "\n".join(lines)


def _default_trade_days(report_date: str) -> list[str]:
    from data.research_store import ResearchStore

    return ResearchStore().trade_days_through(report_date)


def generate_premarket_facts(report_date: str = "", *,
                             trade_days: Optional[Iterable[str]] = None,
                             fetcher: Optional[Callable[..., list[dict]]] = None,
                             store: Optional[PremarketFactStore] = None,
                             now: Optional[datetime] = None) -> dict:
    """生成并持久化当天盘前事实产物；源失败时优先读取已入库窗口缓存。"""
    now = (now or datetime.now(_SHANGHAI)).astimezone(_SHANGHAI)
    report_date = str(report_date or now.date().isoformat())[:10]
    store = store or PremarketFactStore()
    days = list(trade_days) if trade_days is not None else _default_trade_days(report_date)
    window = resolve_window(report_date, days)
    run_id = uuid.uuid4().hex
    started_at = now.isoformat()
    observed_at = started_at
    warnings: list[str] = []
    notes = ["本地产物不调用 LLM，不产生额外 token", "证券映射关闭，selection_effect=reference_only"]
    stages = {
        "source_fetch": "pending", "normalization": "pending", "cache_load": "pending",
        "tagging": "pending", "clustering": "pending", "report_generation": "pending",
    }
    fetched: list[dict] = []
    ingest_metrics = {"inserted": 0, "revised": 0, "observed": 0}
    try:
        from data.sources.cls import red_telegraphs

        fetch = fetcher or red_telegraphs
        fetched = fetch(int(window.start.timestamp()), int(window.end.timestamp()), 40)
        stages["source_fetch"] = "success" if fetched else "empty"
        stages["normalization"] = "success"
        if fetched:
            ingest_metrics = store.ingest(run_id, fetched, window, observed_at)
        else:
            warnings.append("CLS 红讯本次没有返回新记录，已检查本地事实缓存")
    except Exception as exc:
        stages["source_fetch"] = "degraded"
        stages["normalization"] = "skipped"
        warnings.append(f"CLS 红讯抓取失败，已回退本地事实缓存: {type(exc).__name__}")

    events = store.load_window(window)
    stages["cache_load"] = "success" if events else "empty"
    previous_threads = store.load_threads()
    tags = tag_events(events, window, previous_threads)
    store.save_tags(run_id, tags, observed_at)
    stages["tagging"] = "local_rules"
    threads = build_theme_threads(tags, previous_threads, observed_at)
    store.save_threads(threads, observed_at)
    stages["clustering"] = "success"
    brief = _brief_payload(tags, threads, window)
    brief["text"] = render_brief(brief)
    stages["report_generation"] = "success" if events else "incomplete"
    verified = sum(1 for item in brief["facts"] if item.get("evidence_verified"))
    coverage = verified / len(brief["facts"]) if brief["facts"] else 0.0
    if events:
        status = "success_with_warnings" if warnings else "success"
    else:
        status = "incomplete"
        warnings.append("窗口内没有可用 CLS 红讯，晨报应继续使用原有多源新闻兜底")
    finished_at = datetime.now(_SHANGHAI).isoformat()
    result = {
        "run_id": run_id, "report_date": report_date, "status": status,
        "started_at": started_at, "finished_at": finished_at,
        "window": {
            "start": window.start.isoformat(),
            "background_end": window.background_end.isoformat(),
            "end": window.end.isoformat(),
        },
        "stage_status": stages, "warnings": warnings, "data_notes": notes,
        "metrics": {
            "input_count": len(fetched),
            "deduplicated_count": len(events),
            "inserted_count": ingest_metrics["inserted"],
            "revised_count": ingest_metrics["revised"],
            "tagged_count": len(tags),
            "local_fallback_count": len(tags),
            "theme_count": len(threads),
            "evidence_coverage": round(coverage, 4),
        },
        "brief": brief,
    }
    store.save_run(result)
    return result


def latest_premarket_facts(report_date: str = "") -> dict:
    result = PremarketFactStore().latest(report_date)
    if result.get("status") not in _FINAL_STATUSES:
        return result
    brief = result.pop("brief_payload", {})
    return {**result, "brief": brief}
