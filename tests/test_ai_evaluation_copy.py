from agents import ai_evaluation as evaluation


def test_unowned_report_uses_latest_row_per_symbol_and_neutral_labels(monkeypatch):
    monkeypatch.setattr(evaluation, "_fetch_period", lambda days: [
        {"symbol": "600371", "name": "万向德农", "source": "unified_selection",
         "recommended_at": "2026-09-01", "ref_price": 10, "last_price": 18},
        {"symbol": "600371", "name": "万向德农", "source": "unified_selection",
         "recommended_at": "2026-09-20", "ref_price": 15, "last_price": 18},
    ])
    text = evaluation.format_unowned_picks(set())
    assert text.count("600371") == 1
    assert "+20.00%" in text
    assert "错过的机会" not in text and "幸亏没买" not in text


def test_pending_sample_is_labeled_as_float_not_realized_win_rate():
    row = {"hit_target_at": None, "hit_stop_at": None, "close_reason": None,
           "ref_price": 10, "last_price": 11, "realized_pnl_pct": None}
    result = evaluation._evaluate([row], 30)
    text = evaluation.format_report(result)
    assert "样本盈利占比(含未了结浮动)" in text
    assert "真实胜率" not in text
