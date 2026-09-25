"""Dated, versioned observations from the two exchanges' 2026 notices.

These are explicit statements in the notices, not an inferred annual calendar.
New dates require a reviewed notice for each exchange.
"""

from datetime import date, timedelta

NOTICES = {
    "sse_holiday_notice_2026": "https://big5.sse.com.cn/site/cht/www.sse.com.cn/disclosure/announcement/general/c/c_20260915_10832273.shtml",
    "szse_holiday_notice_2026": "https://www.szse.cn/www/disclosure/notice/general/t20260917_622911.html",
}


def evidence(start_date: str, end_date: str) -> dict[str, list[tuple[str, bool]]]:
    closed = set()
    for first, last in ((date(2026, 9, 25), date(2026, 9, 27)),
                        (date(2026, 10, 1), date(2026, 10, 7))):
        day = first
        while day <= last:
            closed.add(day.isoformat())
            day += timedelta(days=1)
    closed.update({"2026-09-20", "2026-10-10"})
    observations = {day: False for day in closed}
    observations.update({"2026-09-28": True, "2026-10-08": True})
    rows = sorted((day, state) for day, state in observations.items()
                  if start_date <= day <= end_date)
    return {provider: rows for provider in NOTICES}
