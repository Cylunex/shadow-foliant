from scripts import daily_signal_scan
from application import market_risk_notice


def test_noon_report_leads_with_down_and_reduce(monkeypatch):
    monkeypatch.setattr(market_risk_notice, "current_indices", lambda now: {"rows": [
        {"name": "上证指数", "change_pct": -1.0},
        {"name": "深证成指", "change_pct": -1.2},
        {"name": "创业板指", "change_pct": -0.8},
    ]})
    monkeypatch.setattr(daily_signal_scan, "get_portfolio_codes", lambda: ["000001", "000002"])
    monkeypatch.setattr(daily_signal_scan.datahub, "quotes", lambda codes: {
        "000001": {"name": "甲", "change_pct": -2.4},
        "000002": {"name": "乙", "change_pct": 0.2},
    })

    text = daily_signal_scan.noon_report()

    assert text.startswith("【A股参考｜午盘｜")
    assert '盘面判断：看跌' in text and '仓位参考：减仓' in text
    assert "涨1只、跌1只" in text
    assert "最弱：甲-2.4%" in text
    assert "TOP10" not in text
    assert '\n\n2）持仓观察\n' in text
    assert len(text) <= 1500


def test_noon_report_defaults_to_no_action_without_a_clear_move(monkeypatch):
    monkeypatch.setattr(market_risk_notice, "current_indices", lambda now: {"rows": [
        {"name": "上证指数", "change_pct": 0.1},
        {"name": "深证成指", "change_pct": -0.1},
        {"name": "创业板指", "change_pct": 0.0},
    ]})
    monkeypatch.setattr(daily_signal_scan, "get_portfolio_codes", lambda: [])

    text = daily_signal_scan.noon_report()

    assert text.startswith("【A股参考｜午盘｜")
    assert '盘面判断：震荡' in text and '仓位参考：不动' in text


def test_missing_market_is_unknown_not_flat(monkeypatch):
    monkeypatch.setattr(market_risk_notice, "current_indices", lambda now: {"rows": []})
    monkeypatch.setattr(daily_signal_scan, "get_portfolio_codes", lambda: [])
    text = daily_signal_scan.noon_report()
    assert '盘面判断：待核验' in text
    assert '指数行情不足' in text and '涨跌都不明显' not in text
