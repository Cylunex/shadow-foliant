from scripts import daily_signal_scan


def test_noon_report_leads_with_down_and_reduce(monkeypatch):
    monkeypatch.setattr(daily_signal_scan.datahub, "indices", lambda: [
        {"name": "上证指数", "change_pct": -1.0},
        {"name": "深证成指", "change_pct": -1.2},
        {"name": "创业板指", "change_pct": -0.8},
    ])
    monkeypatch.setattr(daily_signal_scan, "get_portfolio_codes", lambda: ["000001", "000002"])
    monkeypatch.setattr(daily_signal_scan.datahub, "quotes", lambda codes: {
        "000001": {"name": "甲", "change_pct": -2.4},
        "000002": {"name": "乙", "change_pct": 0.2},
    })

    text = daily_signal_scan.noon_report()

    assert text.startswith("🟢 午盘：看跌｜减仓\n操作：减仓")
    assert "持仓：涨1只、跌1只" in text
    assert "最弱：甲-2.4%" in text
    assert "TOP10" not in text
    assert len(text.splitlines()) <= 6


def test_noon_report_defaults_to_no_action_without_a_clear_move(monkeypatch):
    monkeypatch.setattr(daily_signal_scan.datahub, "indices", lambda: [
        {"name": "上证指数", "change_pct": 0.1},
        {"name": "深证成指", "change_pct": -0.1},
        {"name": "创业板指", "change_pct": 0.0},
    ])
    monkeypatch.setattr(daily_signal_scan, "get_portfolio_codes", lambda: [])

    text = daily_signal_scan.noon_report()

    assert text.startswith("⚪ 午盘：震荡｜不动\n操作：不动")
