# -*- coding: utf-8 -*-
"""data.sources.cninfo —— 巨潮资讯(cninfo.com.cn)直连原子源(阶段 3)。

直连 www.cninfo.com.cn 公告全文检索(POST),**不碰 akshare**。能力:announcements(个股公告)。
契约见 data/sources/README.md。
"""
from __future__ import annotations

from typing import List

from . import _common as C


def announcements(code: str, page_size: int = 30) -> List[dict]:
    """巨潮个股公告全文检索 → [{title,type,date,url}]。空/异常 → []。"""
    c = C.norm_code(code)
    if c.startswith("6"):
        org_id = f"gssh0{c}"
    elif c.startswith("8") or c.startswith("4"):
        org_id = f"gsbj0{c}"
    else:
        org_id = f"gssz0{c}"
    payload = {
        "stock": f"{c},{org_id}", "tabName": "fulltext", "pageSize": str(page_size), "pageNum": "1",
        "column": "", "category": "", "plate": "", "seDate": "", "searchkey": "", "secid": "",
        "sortName": "", "sortType": "", "isHLtitle": "true",
    }
    headers = {"User-Agent": C.DESKTOP_UA, "Content-Type": "application/x-www-form-urlencoded",
               "Referer": "https://www.cninfo.com.cn/new/disclosure", "Origin": "https://www.cninfo.com.cn"}
    try:
        d = C.requests_session().post("https://www.cninfo.com.cn/new/hisAnnouncement/query",
                                      data=payload, headers=headers, timeout=15).json()
        return [{"title": it.get("announcementTitle", ""), "type": it.get("announcementTypeName", ""),
                 "date": it.get("announcementTime", ""),
                 "url": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={it.get('announcementId', '')}"}
                for it in (d.get("announcements", []) or [])]
    except Exception as e:
        print(f"[sources.cninfo] 巨潮公告请求失败: {e}")
        return []


if __name__ == '__main__':
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== data.sources.cninfo 直连自检 ===')
    a = announcements('600519', 5)
    print(f'announcements 600519: {len(a)} 条;', (a[0]['title'][:40] if a else None))
    print('OK' if a else '⚠️ 空(可能网络/被封)')
