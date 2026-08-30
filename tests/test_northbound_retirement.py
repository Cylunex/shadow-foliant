from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_northbound_runtime_entry_points_are_retired():
    contracts = {
        "data/datahub.py": ("def north_flow(", "north_flow ="),
        "mcp_server.py": ("def north_flow(", '"north_flow"'),
        "webui/api_server.py": ("/api/market/north",),
        "webui/static/pages/market.js": ("north-refresh", "northFlow"),
        "agents/agent_tool_groups.py": ('"north_flow"', "datahub.north_flow"),
    }
    for relative, forbidden in contracts.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in source, f"{relative} still exposes {marker}"


def test_removed_cache_module_is_not_present():
    assert not (ROOT / "data/northbound_cache.py").exists()
