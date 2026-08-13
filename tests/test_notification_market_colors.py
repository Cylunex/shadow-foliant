from pathlib import Path

from analysis.market_add_signal import ACTION_META
from analysis.pattern_recognition import TALIB_PATTERNS
from portfolio.exit_advisor import _ACT_TAG
from portfolio.portfolio_health_ai import _ACT_EMOJI


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_trade_actions_follow_a_share_red_up_green_down():
    assert ACTION_META["strong_buy"][0].startswith("🔴")
    assert ACTION_META["buy"][0].startswith("🔴")
    assert ACTION_META["reduce"][0].startswith("🟢")
    assert ACTION_META["sell"][0].startswith("🟢")
    assert _ACT_EMOJI["add"].startswith("🔴")
    assert _ACT_EMOJI["reduce"].startswith("🟢")
    assert _ACT_EMOJI["sell"].startswith("🟢")
    assert _ACT_TAG["reduce"].startswith("🟢")
    assert _ACT_TAG["sell"].startswith("🟢")


def test_candlestick_direction_uses_a_share_colors():
    assert TALIB_PATTERNS["hammer"][1] == "🔴看涨"
    assert TALIB_PATTERNS["morning_star"][1] == "🔴看涨"
    assert TALIB_PATTERNS["shooting_star"][1] == "🟢看跌"
    assert TALIB_PATTERNS["three_black_crows"][1] == "🟢看跌"


def test_primary_notification_templates_have_no_reversed_direction_labels():
    sources = "\n".join(_read(path) for path in (
        "jobs/jobs_hub.py",
        "portfolio/position_guardian.py",
        "portfolio/portfolio_health_ai.py",
        "portfolio/exit_advisor.py",
        "analysis/market_add_signal.py",
        "analysis/pattern_recognition.py",
        "scripts/daily_signal_scan.py",
    ))
    forbidden = (
        "🟢【今日组合动作：适度买入】",
        "🔴【今日组合动作：优先卖出】",
        "🔴清仓",
        "🔴减仓",
        "🟢加仓",
        "🟢看涨",
        "🔴看跌",
        "🟢 **涨幅TOP5**",
        "🔴 **跌幅TOP5**",
        "━━━ 🔴 建议减仓",
        "━━━ 🟢 建议加仓",
    )
    for label in forbidden:
        assert label not in sources


def test_email_and_generic_cards_do_not_apply_western_market_colors():
    email = _read("notify/notification_service.py")
    router = _read("notify/notification_router.py")
    assert ".rating-buy {{ color: #df3448" in email
    assert ".rating-sell {{ color: #10a36a" in email
    assert "'color': 0x4F7CFF" in router
    assert "'color': '#4F7CFF'" in router


def test_risk_severity_avoids_market_red_green():
    portfolio_rules = _read("analysis/portfolio_rules.py")
    shadow_account = _read("analysis/shadow_account.py")
    expected = 'icon = {"alert": "⛔", "warn": "⚠️", "info": "✅"}'
    assert expected in portfolio_rules
    assert expected in shadow_account
