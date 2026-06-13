# -*- coding: utf-8 -*-
"""独立晨报推送 — 不依赖 webui/jobs 常驻,适合 Windows 任务计划/cron 每日调用。
用法: python scripts/push_briefing.py
非交易日自动跳过(加 --force 强制)。
"""
import os
import sys
import io
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import _bootstrap  # noqa: F401  注入 sys.path
try:
    from dotenv import load_dotenv
    # 显式从项目根读 .env(任务计划/cron 的 cwd 可能不在项目目录)
    load_dotenv(os.path.join(_ROOT, ".env"))
except Exception:
    pass


def _is_trading_day() -> bool:
    try:
        import akshare as ak
        import datetime
        df = ak.tool_trade_date_hist_sina()
        col = df.columns[0]
        today = datetime.date.today().isoformat()
        return today in {str(d) for d in df[col]}
    except Exception:
        # 取不到日历 → 只按周末判断
        import datetime
        return datetime.date.today().weekday() < 5


if __name__ == "__main__":
    force = "--force" in sys.argv
    if not force and not _is_trading_day():
        print(json.dumps({"skipped": "非交易日"}, ensure_ascii=False))
        sys.exit(0)
    from briefing import run_and_push
    r = run_and_push()
    print(json.dumps(r, ensure_ascii=False))
