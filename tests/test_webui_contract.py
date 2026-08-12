from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_a_share_market_colors_are_explicit_and_consistent():
    styles = _read("webui/static/styles.css")
    lib = _read("webui/static/lib.js")
    app = _read("webui/static/app.js")
    trade = _read("webui/static/pages/trade.js")

    assert "--market-up:#f04455" in styles
    assert "--market-down:#10a36a" in styles
    assert "Number(v)>0?'red':'green'" in lib
    assert "'红涨'" in app
    assert "'绿跌'" in app
    assert "x.trade_type==='买入'?'red':'green'" in trade


def test_webui_defaults_to_agent_cockpit_with_grouped_navigation():
    app = _read("webui/static/app.js")

    assert "location.hash || '#cockpit'" in app
    assert "const NAV_GROUPS" in app
    for group in ("工作台", "资产管理", "投研中心", "策略与自动化"):
        assert f"title:'{group}'" in app


def test_latest_selection_and_live_strategy_are_first_class_web_capabilities():
    api_server = _read("webui/api_server.py")
    screen = _read("webui/static/pages/screen.js")
    genome = _read("webui/static/pages/genome.js")

    assert '@app.get("/api/screen/latest")' in api_server
    assert '@app.get("/api/strategy-genome/deployment")' in api_server
    assert "最终优选 TOP5" in screen
    assert "完整候选 TOP15" in screen
    assert "当前生产部署集" in genome


def test_static_assets_are_present_and_versioned():
    index = _read("webui/static/index.html")
    manifest = _read("webui/static/manifest.webmanifest")
    service_worker = _read("webui/static/sw.js")

    assert 'styles.css?v=8' in index
    assert 'app.js?v=8' in index
    assert "A股" in manifest
    assert "sf-shell-v2" in service_worker
    for asset in ("app.js", "styles.css", "manifest.webmanifest", "sw.js"):
        assert (STATIC / asset).is_file()
