from decimal import Decimal, ROUND_FLOOR

import pandas as pd
import pytest

from analysis import trade_plan


@pytest.mark.parametrize("code,close,atr,target", [
    ("510300", 0.813, 0.013, 0.851),
    ("600001", 10., 0.137, 10.532),
    ("600002", 500., 1.375, 504.46),
])
def test_published_plan_levels_and_rr_gate_use_same_prices(monkeypatch, code, close, atr, target):
    frame = pd.DataFrame({"Open": close, "High": close + atr, "Low": close - atr,
                          "Close": close, "Volume": 1000.},
                         index=pd.bdate_range(end="2026-10-14", periods=80))
    monkeypatch.setattr(trade_plan, "_atr", lambda _: atr)
    monkeypatch.setattr(trade_plan, "evaluate_multi_timeframe", lambda _: {
        "weekly_regime": "bullish", "daily_trigger": True, "resonance": "confirmed"})
    monkeypatch.setattr(trade_plan, "analyze_levels", lambda *_: {
        "levels": {"resistance": [{"value": target}]}})
    monkeypatch.setattr(trade_plan, "analyze_risk", lambda _: {"var_hist": 0.02})
    plan = trade_plan.build_trade_plan(code, frame, technical_state={"grade": "good"},
                                       market_signal={"action": "buy"})
    midpoint = (Decimal(str(plan["entry_low"])) + Decimal(str(plan["entry_high"]))) / 2
    rr = (Decimal(str(plan["target_price"])) - midpoint) / (midpoint - Decimal(str(plan["stop_loss"])))
    assert plan["risk_reward_ratio"] == float(rr.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))
    assert (plan["candidate_action"] == "buy") is (rr >= 2)
    if code == "510300":
        assert plan["current_price"] == 0.813
        assert plan["reference_price_decimals"] == 3
    else:
        assert plan["reference_price_decimals"] == 2
    unknown = trade_plan.build_trade_plan(code, frame, technical_state={"grade": "good"})
    assert unknown["candidate_action"] == "watch"
    assert unknown["suggested_position_pct"] == 0
    assert any("组合环境尚未核验" in reason for reason in unknown["blockers"])
