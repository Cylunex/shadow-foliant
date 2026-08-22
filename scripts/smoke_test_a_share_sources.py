"""A 股分钟行情脱敏冒烟：不输出 Token、节点、价格、成交量或响应正文。"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _bootstrap  # noqa: F401
import datahub
from data.sources import easy_tdx, tdx_python, zzshare


def _summary(name, available, frame):
    if frame is None or getattr(frame, "empty", True):
        return {"source": name, "configured": bool(available), "ok": False, "rows": 0}
    times = frame["date"] if "date" in frame.columns else frame.index
    return {
        "source": name,
        "configured": bool(available),
        "ok": True,
        "rows": int(len(frame)),
        "first_at": str(times.min()),
        "last_at": str(times.max()),
        "columns": list(frame.columns),
    }


def main() -> int:
    # 第三方库的连接诊断可能含节点或响应正文，冒烟只保留结构化脱敏结论。
    logging.disable(logging.CRITICAL)
    results = []

    zz_ready = zzshare.available()
    zz_frame = zzshare.get_kline("600000", frequency="1m", count=5) if zz_ready else None
    results.append(_summary("zzshare", zz_ready, zz_frame))

    tdx_ready = tdx_python.available()
    tdx_frame = tdx_python.get_kline("600000", frequency="1m", count=5) if tdx_ready else None
    tdx_summary = _summary("tdx_python", tdx_ready, tdx_frame)
    if tdx_ready:
        tdx_summary['daily_ok'] = not tdx_python.get_kline(
            "600000", frequency="1d", count=5
        ).empty
        tdx_summary['quote_ok'] = bool(tdx_python.get_quote("600000"))
    results.append(tdx_summary)

    compat_ready = easy_tdx.available()
    compat_frame = easy_tdx.get_kline("600000", frequency="1m", count=5) if compat_ready else None
    results.append(_summary("easy_tdx_compat", compat_ready, compat_frame))

    routed = datahub.kline("600000", period="1mo", interval="1m", use_cache=False)
    routed_summary = _summary(
        str(getattr(routed, 'attrs', {}).get('datahub_source', 'datahub')),
        True,
        routed,
    )
    routed_summary['route'] = 'datahub_intraday'
    results.append(routed_summary)

    for item in results:
        print(item)
    return 0 if any(item["ok"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
