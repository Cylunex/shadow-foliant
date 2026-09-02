from notify.plain_language import (
    build_portfolio_message,
    build_market_message,
    compact_notification,
    normalize_action,
    normalize_direction,
    plain_text,
)
from notify import notification_router


def test_market_message_leads_with_direction_and_action():
    title, body = build_market_message(
        label="午盘",
        direction="偏弱",
        action="先卖一点",
        market="上证指数 -1.2%，MA60 下方",
        holdings="涨2只、跌6只",
        reason="VaR95 偏高，缠论三卖",
        as_of="12:00",
    )
    assert title == "🟢 午盘：看跌｜减仓"
    assert body.splitlines()[0] == "操作：减仓"
    assert "60日均线" in body
    assert "短期风险" in body
    assert "走势转弱" in body
    assert "VaR" not in body
    assert "缠论" not in body


def test_direction_and_action_are_strictly_normalized():
    assert normalize_direction("偏强") == "看涨"
    assert normalize_direction("不涨不跌") == "震荡"
    assert normalize_direction("没有明显方向") == "震荡"
    assert normalize_action("建议清仓") == "减仓"
    assert normalize_action("回落可以买") == "加仓"
    assert normalize_action("strong_buy") == "加仓"
    assert normalize_action("继续观察") == "不动"


def test_instant_notification_is_short_but_archive_is_untouched():
    content = "\n".join(["#### 专业报告", "━━━━"] + [f"第{i}行 MA60 VaR95" for i in range(20)])
    compact = compact_notification("report", content)
    assert len(compact.splitlines()) == 8
    assert "专业报告" in compact
    assert "60日均线" in compact
    assert "短期风险" in compact
    assert compact_notification("archive", content) == content


def test_portfolio_message_only_exposes_plain_actions_and_moves():
    title, body = build_portfolio_message(
        label="早盘持仓",
        signal={"action": "buy", "median_change": -1.0,
                "reason": "MA20 附近普跌，未连续下杀"},
        sell_rows=[{"code": "000001", "name": "甲", "change": -2.1,
                    "sell_reasons": ["破MA60"]}],
        buy_rows=[{"code": "000002", "name": "乙", "change": 0.8,
                   "buy_reason": "走势转强"}],
        market="三大指数都在跌",
    )
    assert title == "🟢 早盘持仓：看跌｜加仓"
    assert "减仓：甲｜跌2.1%｜跌破60日均线" in body
    assert "加仓：乙｜涨0.8%｜走势转强" in body
    assert "MA" not in body


def test_router_compacts_content_before_delivery(monkeypatch):
    delivered = {}

    def fake_sender(title, content):
        delivered["title"] = title
        delivered["content"] = content
        return True, "ok"

    monkeypatch.setitem(notification_router.CHANNELS, "fake", fake_sender)
    content = "\n".join(["## 报告", "━━━━"] + [f"第{i}行 MA60 VaR95" for i in range(12)])
    result = notification_router.send("report", "测试", content, only_channels=["fake"])

    assert result["fake"][0] is True
    assert len(delivered["content"].splitlines()) == 8
    assert "60日均线" in delivered["content"]
    assert "VaR" not in delivered["content"]


def test_buy_rows_survive_real_report_delivery_with_many_sell_rows(monkeypatch):
    delivered = {}
    def send(title, content):
        delivered["body"] = content
        return True, "ok"
    monkeypatch.setitem(notification_router.CHANNELS, "fake", send)
    sells = [{"code": f"S{i}", "name": f"减仓股{i}", "sell_reasons": ["破MA60"]}
             for i in range(5)]
    buys = [{"code": f"B{i}", "name": f"加仓股{i}", "buy_reason": "走势转强"}
            for i in range(8)]
    title, body = build_portfolio_message(
        label="早盘持仓", signal={"action": "strong_buy", "median_change": -2.03,
                                  "reason": "全市场急跌"},
        sell_rows=sells, buy_rows=buys, market="市场下跌", as_of="10:05",
    )
    notification_router.send("report", title, body, only_channels=["fake"])
    actual = delivered["body"]
    assert "减仓关注5只、加仓候选8只" in actual
    assert "加仓：加仓股0" in actual and "加仓：加仓股1" in actual
    assert "减仓：减仓股0" in actual
    assert len(actual.splitlines()) <= 8
    assert actual.index("加仓：") < actual.index("减仓：")


def test_conflicting_and_duplicate_rows_do_not_inflate_buy_count():
    row = {"code": "A", "name": "冲突股", "sell_score": 2}
    _, body = build_portfolio_message(
        label="早盘持仓", signal={"action": "buy"},
        sell_rows=[row, row], buy_rows=[row, row],
    )
    assert "减仓关注1只、加仓候选0只" in body
    assert "暂无合适个股，先等" in body
    assert "加仓：" not in body


def test_zero_item_limit_does_not_emit_an_item():
    _, body = build_portfolio_message(
        label="早盘持仓", signal={"action": "buy"},
        buy_rows=[{"code": "A", "name": "示例"}], item_limit=0,
    )
    assert "加仓：" not in body


def test_task_identifiers_are_not_destroyed_by_markdown_cleanup():
    assert plain_text("research_data_sync_premarket_retry") == "research_data_sync_premarket_retry"
    assert plain_text("_说明_ **重点**") == "说明 重点"
