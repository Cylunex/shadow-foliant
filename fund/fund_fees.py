"""Shared channel fee rules; operating expenses already reflected in NAV are excluded."""

from datetime import date
from fund.fund_contracts import decimal, number


def redemption_charge(lots, nav, as_of, schedule):
    if not schedule:
        raise ValueError("赎回费阶梯缺失")
    bands = sorted(schedule, key=lambda x: int(x["min_days"]), reverse=True)
    if not any(int(b["min_days"]) == 0 for b in bands):
        raise ValueError("费率阶梯须包含零日档")
    for band in bands:
        if int(band["min_days"]) < 0 or not 0 <= number(band["rate"]) < 1:
            raise ValueError("无效费率阶梯")
    total = decimal(0)
    details = []
    for lot in lots:
        if not lot.get("date"):
            raise ValueError("期初批次缺少真实持有日期，无法估算赎回费")
        age = (
            date.fromisoformat(str(as_of)[:10])
            - date.fromisoformat(str(lot["date"])[:10])
        ).days
        if age < 0:
            raise ValueError("批次持有期在未来")
        rate = next(b["rate"] for b in bands if age >= int(b["min_days"]))
        gross = decimal(float(lot["shares"]) * float(nav))
        charge = decimal(gross * decimal(rate, "0.00000001"))
        total += charge
        details.append(
            {
                "date": str(lot["date"])[:10],
                "days": age,
                "shares": float(lot["shares"]),
                "rate": float(rate),
                "gross": float(gross),
                "fee": float(charge),
            }
        )
    return {"fee": float(total), "lots": details, "basis": "持有批次与渠道费率阶梯"}


def share_class_comparison(classes, amount, holding_days):
    """Forward fee scenarios only. Never deduct operating fees again from historical NAV."""
    if int(holding_days) < 1:
        raise ValueError("持有天数必须大于零")
    gross = number(amount, positive=True)
    rows = []
    family = {x.get("family_id") for x in classes}
    if len(family) != 1 or None in family:
        raise ValueError("份额类别须有已核实的相同基金家族")
    for item in classes:
        required = ("purchase_rate", "annual_service_rate", "redemption_schedule")
        if any(item.get(k) is None for k in required):
            rows.append(
                {"code": item["code"], "status": "partial", "reason": "费率字段缺失"}
            )
            continue
        purchase = number(item["purchase_rate"])
        annual = number(item["annual_service_rate"])
        if not 0 <= purchase < 1 or not 0 <= annual < 1:
            raise ValueError("无效费率")
        net = gross / (1 + purchase)
        service = net * annual * int(holding_days) / 365
        from datetime import date, timedelta

        held = date(2020, 1, 1)
        charge = redemption_charge(
            [{"date": held.isoformat(), "shares": net - service}],
            1.0,
            held + timedelta(days=int(holding_days)),
            item["redemption_schedule"],
        )
        rows.append(
            {
                "code": item["code"],
                "status": "ready",
                "purchase_fee": gross - net,
                "service_fee_scenario": service,
                "redemption_fee_scenario": charge["fee"],
                "estimated_cost": gross - net + service + charge["fee"],
            }
        )
    return {
        "basis": "未来持有期费用静态情景，假定资产价格不变，不扣减历史净值",
        "holding_days": int(holding_days),
        "amount": gross,
        "classes": rows,
    }
