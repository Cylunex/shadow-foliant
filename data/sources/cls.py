# -*- coding: utf-8 -*-
"""data.sources.cls —— 财联社(cls.cn)直连原子源。

直连 www.cls.cn，**不碰 akshare**。能力：

- ``telegraph``：兼容原有实时快讯入口；
- ``red_telegraphs``：按时间窗口分页读取加红电报，并保留稳定源 ID。

契约见 data/sources/README.md。
"""
from __future__ import annotations

import hashlib
import html
import re
import time
from datetime import datetime
from typing import Any, List
from zoneinfo import ZoneInfo

from . import _common as C


_CLS_APP = "CailianpressWeb"
_CLS_WEB_VERSION = "8.7.9"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _red_signature(params: dict[str, Any]) -> str:
    """生成 CLS Web 请求签名；只依赖公开请求参数，不保存 Cookie。"""
    canonical = "&".join(
        f"{key}={params[key]}" for key in sorted(params, key=lambda value: str(value).upper())
    )
    first = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    return hashlib.md5(first.encode("utf-8")).hexdigest()


def _red_params(last_time: int, page_size: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "app": _CLS_APP,
        "category": "red",
        "last_time": int(last_time),
        "os": "web",
        "refresh_type": 1,
        "rn": max(1, min(int(page_size), 100)),
        "sv": _CLS_WEB_VERSION,
    }
    params["sign"] = _red_signature(params)
    return params


def _clean(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _normalize_red_item(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        source_id = str(int(item.get("id") or 0))
        published_ts = int(item.get("ctime") or 0)
    except (TypeError, ValueError):
        return None
    if source_id == "0" or published_ts <= 0:
        return None
    raw_content = _clean(item.get("content") or item.get("brief"))
    title = _clean(item.get("title") or item.get("brief"))
    match = re.match(r"^【([^】]{2,80})】\s*(.*)$", raw_content)
    if match:
        title = match.group(1).strip()
        content = match.group(2).strip()
    else:
        content = raw_content
    if not title:
        title = content[:80]
    return {
        "source": "cls_red",
        "source_event_id": source_id,
        "title": title,
        "content": content or raw_content,
        "published_at": datetime.fromtimestamp(published_ts, _SHANGHAI).isoformat(),
        "published_ts": published_ts,
        "level": str(item.get("level") or ""),
        "url": f"https://www.cls.cn/detail/{source_id}",
        "source_quality": "primary_fact",
        "raw_payload": item,
    }


def telegraph(page_size: int = 50) -> List[dict]:
    """财联社电报(全市场实时快讯)→ [{title,content,time}]。空/异常 → []。"""
    try:
        d = C.requests_session().get("https://www.cls.cn/nodeapi/telegraphList",
                                     params={"rn": str(page_size), "page": "1"},
                                     headers={"User-Agent": C.DESKTOP_UA, "Referer": "https://www.cls.cn/"},
                                     timeout=10).json()
        return [{"title": it.get("title", "") or it.get("brief", ""),
                 "content": it.get("content", "") or it.get("brief", ""),
                 "time": it.get("ctime", "")}
                for it in (d.get("data", {}) or {}).get("roll_data", [])]
    except Exception as e:
        print(f"[sources.cls] 财联社快讯请求失败: {e}")
        return []


def red_telegraphs(start_ts: int, end_ts: int, max_pages: int = 40,
                   page_size: int = 20) -> List[dict]:
    """读取 ``[start_ts, end_ts]`` 内的财联社加红电报。

    与 ``telegraph`` 不同，本入口在网络或协议异常时抛出异常，方便上层明确记录
    ``source_fetch=degraded`` 并回退到已持久化事件，而不是把失败伪装成业务空结果。
    """
    start_ts, end_ts = int(start_ts), int(end_ts)
    if start_ts > end_ts:
        raise ValueError("start_ts must be <= end_ts")
    session = C.requests_session()
    rows: dict[str, dict[str, Any]] = {}
    cursor_time = end_ts
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.cls.cn/telegraph",
        "User-Agent": C.DESKTOP_UA,
    }
    for page in range(max(1, int(max_pages))):
        response = session.get(
            "https://www.cls.cn/v1/roll/get_roll_list",
            params=_red_params(cursor_time, page_size),
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("errno", -1)) != 0:
            raise RuntimeError("CLS red telegraph API rejected the request")
        items = ((payload.get("data") or {}).get("roll_data") or [])
        if not items:
            break
        oldest = cursor_time
        for item in items:
            normalized = _normalize_red_item(item) if isinstance(item, dict) else None
            if not normalized:
                continue
            published_ts = int(normalized["published_ts"])
            oldest = min(oldest, published_ts)
            if start_ts <= published_ts <= end_ts:
                rows[normalized["source_event_id"]] = normalized
        if oldest <= start_ts or oldest >= cursor_time:
            break
        cursor_time = oldest
        if page + 1 < max_pages:
            time.sleep(0.1)
    return sorted(rows.values(), key=lambda item: (item["published_ts"], item["source_event_id"]))


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.cls 直连自检 ===')
    t = telegraph(5)
    print(f'telegraph: {len(t)} 条;', (t[0]['title'][:40] if t else None))
    print('OK' if t else '⚠️ 空(可能网络/被封)')
