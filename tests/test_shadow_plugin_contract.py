from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
PLATFORM_ROOT = ROOT.parent / "shadow-platform"
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))


def _application_routes(app) -> dict[tuple[str, str], str | None]:
    collected = {}
    for route in app.router.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in getattr(route, "methods", set()) or set():
            collected[(path, method)] = getattr(route, "operation_id", None)
    return collected


def test_plugin_definition_manifest_and_project_versions_match() -> None:
    from shadow_sdk.plugin_contracts import validate_plugin

    plugin = validate_plugin(ROOT, PLATFORM_ROOT)
    version = (ROOT / "VERSION").read_text("utf-8").strip()

    assert plugin.plugin_id == "shadow-foliant"
    assert plugin.version == version == plugin.agent_manifest["package_version"]
    assert plugin.definition["spec"]["compatibility"]["dsh"] == {
        "distribution": ">=0.1.1-rc.2 <0.2.0",
        "tools_api": ">=0.1.1-rc.2 <0.2.0",
    }


def test_agent_openapi_routes_and_operation_ids_match_fastapi() -> None:
    from webui.api_server import app

    contract = yaml.safe_load((ROOT / "contracts" / "agent.openapi.yaml").read_text("utf-8"))
    actual = _application_routes(app)
    for path, path_item in contract["paths"].items():
        for method, operation in path_item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            assert (path, method.upper()) in actual
            assert actual[(path, method.upper())] == operation["operationId"]


def test_research_profile_is_explicit_and_has_no_private_finance_capability() -> None:
    profile = yaml.safe_load(
        (ROOT / "agent" / "profiles" / "shadow-finance-research.yaml").read_text("utf-8")
    )
    capabilities = profile["plugins"][0]["capabilities"]
    assert capabilities != "*"
    assert len(capabilities) == 7
    rendered = "\n".join(capabilities)
    assert not re.search(r"portfolio|trade|watchlist|alert|operations|broker", rendered)
    assert profile["runtime"]["distribution_version"] == "0.1.1-rc.2"
    assert profile["runtime"]["tools_api_version"] == "0.1.1-rc.2"


def test_skills_and_descriptors_are_local_complete_and_runtime_neutral() -> None:
    definition = yaml.safe_load((ROOT / "shadow-plugin.yaml").read_text("utf-8"))
    manifest = yaml.safe_load((ROOT / "agent" / "manifest.yaml").read_text("utf-8"))
    for relative in definition["spec"]["descriptors"].values():
        assert (ROOT / relative).is_file()
    for skill in manifest["skills"]:
        skill_path = ROOT / "agent" / skill["path"]
        assert skill_path.is_file()
        for reference in re.findall(r"\]\((references/[^)]+)\)", skill_path.read_text("utf-8")):
            assert (skill_path.parent / reference).is_file()

    imported = set()
    for path in (ROOT / "application").glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0].lower() for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0].lower())
    assert imported.isdisjoint({"mcp_server", "fastapi", "deepseek-ai", "cordis", "dsh"})


def test_agent_http_layer_never_imports_mcp_server_and_manifest_has_bounded_tools() -> None:
    source = (ROOT / "webui" / "api_server.py").read_text("utf-8")
    machine_section = source[source.index('@app.get("/api/machine/runtime-health")'):
                             source.index("# ============================ 股票(首页)")]
    assert "mcp_server" not in machine_section

    manifest = yaml.safe_load((ROOT / "agent" / "manifest.yaml").read_text("utf-8"))
    tools = [tool for capability in manifest["capabilities"] for tool in capability["tools"]]
    assert 8 <= len(tools) <= 12
    for tool in tools:
        assert 256 <= tool["max_result_bytes"] <= 1048576
        assert 128 <= tool["max_model_chars"] <= 100000
        assert tool["timeout_ms"] <= 300000
        assert tool["contract_ref"] == "contracts/agent.openapi.yaml"


def test_mcp_and_http_compatibility_adapters_share_application_use_cases() -> None:
    mcp_source = (ROOT / "mcp_server.py").read_text("utf-8")
    http_source = (ROOT / "webui" / "api_server.py").read_text("utf-8")

    assert mcp_source.count(".security_research.compatibility_research(") == 1
    assert http_source.count(".security_research.compatibility_research(") == 1
    assert mcp_source.count(".selection.latest_formal()") == 1
    assert http_source.count(".selection.latest_formal()") == 1


def test_definition_and_contracts_contain_only_example_server_values() -> None:
    files = [ROOT / "shadow-plugin.yaml", *(ROOT / "contracts").glob("*.yaml"),
             ROOT / "agent" / "instances.example.yaml"]
    text = "\n".join(path.read_text("utf-8") for path in files)
    assert "https://stock.example.com" in text
    assert not re.search(r"https?://(?!stock\.example\.com)[^\s]+", text)
    assert not re.search(r"(?i)(token|secret|password)\s*:\s*[^\s$<{]+", text)
