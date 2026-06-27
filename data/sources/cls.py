# -*- coding: utf-8 -*-
"""data.sources.cls —— 财联社(cls.cn)直连原子源(阶段 3)。

直连 www.cls.cn nodeapi,**不碰 akshare**。能力:telegraph(全市场实时快讯电报)。
契约见 data/sources/README.md。
"""
from __future__ import annotations

from typing import List

from . import _common as C


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


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.cls 直连自检 ===')
    t = telegraph(5)
    print(f'telegraph: {len(t)} 条;', (t[0]['title'][:40] if t else None))
    print('OK' if t else '⚠️ 空(可能网络/被封)')
