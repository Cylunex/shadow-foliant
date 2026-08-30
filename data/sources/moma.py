"""MOMA API optional reference and fallback source."""

from __future__ import annotations

from ._mairui_compatible import MairuiCompatibleSource
from ._path_token_api import PathTokenApi


_source = MairuiCompatibleSource(
    PathTokenApi(
        provider="moma",
        token_env="MOMA_TOKEN",
        enabled_env="MOMA_ENABLED",
        base_url_env="MOMA_BASE_URL",
        default_base_url="https://api.momaapi.com",
        state_dir_env="MOMA_STATE_DIR",
        budget_env="MOMA_DAILY_REQUEST_BUDGET",
    ),
)

available = _source.available
get_quotes = _source.quotes
get_kline = _source.kline
capital_flow = _source.capital_flow
request_budget_status = _source.budget_status
_reset_for_tests = _source.reset_for_tests
