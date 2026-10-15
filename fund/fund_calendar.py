"""Deterministic plan scheduling. Exchange/product calendar is supplied by caller."""

from datetime import date, timedelta
import calendar


def scheduled_dates(start, end, period="monthly", day_of=1, anchor_date=None):
    start, end = date.fromisoformat(str(start)[:10]), date.fromisoformat(str(end)[:10])
    if period not in ("daily", "weekly", "biweekly", "monthly"):
        raise ValueError("无效定投周期")
    if (
        not 1
        <= int(day_of)
        <= (
            28 if period == "monthly" else 7 if period in ("weekly", "biweekly") else 31
        )
    ):
        raise ValueError("无效定投日")
    if period == "biweekly" and not anchor_date:
        raise ValueError("双周计划必须提供锚点日期")
    anchor = date.fromisoformat(str(anchor_date or start)[:10])
    current = start
    while current <= end:
        due = period == "daily"
        if period == "monthly":
            due = current.day == min(
                int(day_of), calendar.monthrange(current.year, current.month)[1]
            )
        if period in ("weekly", "biweekly"):
            due = current.isoweekday() == int(day_of)
            if period == "biweekly":
                monday = current - timedelta(days=current.weekday())
                base = anchor - timedelta(days=anchor.weekday())
                due = (
                    due
                    and (monday - base).days >= 0
                    and ((monday - base).days // 7) % 2 == 0
                )
        if due and current >= anchor:
            yield current
        current += timedelta(days=1)


def executions(start, end, open_dates, period="monthly", day_of=1, anchor_date=None):
    opened = sorted({date.fromisoformat(str(d)[:10]) for d in open_dates})
    result = []
    for scheduled in scheduled_dates(start, end, period, day_of, anchor_date):
        actual = next((d for d in opened if d >= scheduled), None)
        if actual is not None and actual <= date.fromisoformat(str(end)[:10]):
            result.append(
                {
                    "scheduled_date": scheduled.isoformat(),
                    "execution_date": actual.isoformat(),
                }
            )
    return result
