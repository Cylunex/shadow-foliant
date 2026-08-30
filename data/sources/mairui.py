"""Mairui API optional reference and fallback source."""

from __future__ import annotations

from ._mairui_compatible import MairuiCompatibleSource
from ._path_token_api import PathTokenApi


_source = MairuiCompatibleSource(
    PathTokenApi(
        provider="mairui",
        token_env="MAIRUI_LICENCE",
        enabled_env="MAIRUI_ENABLED",
        base_url_env="MAIRUI_BASE_URL",
        default_base_url="https://api.mairuiapi.com",
        state_dir_env="MAIRUI_STATE_DIR",
        budget_env="MAIRUI_DAILY_REQUEST_BUDGET",
    ),
    pro_env="MAIRUI_QUANT_PRO_ENABLED",
)

available = _source.available
get_quotes = _source.quotes
get_kline = _source.kline
capital_flow = _source.capital_flow
announcements = _source.announcements
interactive_qa = _source.interactive_qa
limit_performance = _source.limit_performance
auction_performance = _source.auction_performance
request_budget_status = _source.budget_status
_reset_for_tests = _source.reset_for_tests
