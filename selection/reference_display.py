"""Read-only external selection display shared by Web and Agent transports."""
from __future__ import annotations

import os
import re
from typing import Any

def openapi_trial_reference(
    shadow: dict[str, Any] | None, *, cached_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Display-only replacement for the failed legacy reference, never a formal input."""
    from selection.wencai_query_contract import ORDER

    payload = shadow if isinstance(shadow, dict) else {}
    awaiting_artifact = not payload
    groups = {row.get('name'): row for row in (payload.get('groups') or [])
              if isinstance(row, dict) and row.get('name') in ORDER}
    cached_names = cached_names or {}
    strategies = {}
    for name in ORDER:
        row = groups.get(name) or {}
        has_data = bool(row.get('schema_valid') and row.get('picks'))
        semantic_ok = bool(row.get('status') == 'complete' and row.get('semantic_verified'))
        provider_names = {
            str(item.get('symbol') or ''): str(item.get('name') or '').strip()
            for item in (row.get('pick_details') or []) if isinstance(item, dict)
        }
        strategies[name] = {
            'strategy_id': 'iwencai_openapi_trial_' + name,
            'strategy_version': 'iwencai-openapi-trial-v1',
            'query_hash': row.get('query_hash'),
            'status': 'pending' if awaiting_artifact else
                      'trial_verified' if semantic_ok else
                      'trial_unverified' if has_data else 'failed',
            'failure_code': None if semantic_ok else 'semantic_unverified' if has_data else
                            'awaiting_daily_shadow_artifact' if awaiting_artifact else
                            str(row.get('status') or 'shadow_not_available')[:48],
            'started_at': row.get('requested_at'),
            'finished_at': row.get('finished_at') or payload.get('sampled_at')
                           or payload.get('executed_at'),
            'result_as_of': row.get('data_as_of'),
            'expected_market_as_of': row.get('market_as_of_expected') or payload.get('expected_market_as_of'),
            'verification_reasons': list(row.get('failure_reasons') or []),
            'picks': [{'symbol': str(symbol),
                       'name': provider_names.get(str(symbol))
                               or cached_names.get(str(symbol), ''),
                       'source_labels': ['问财OpenAPI·试运行']}
                      for symbol in (row.get('picks') or [])[:5]
                      if isinstance(symbol, str) and re.fullmatch(r'\d{6}', symbol)],
            'reference_affects_membership': False,
        }
    verified_groups = sum(row['status'] == 'trial_verified' for row in strategies.values())
    data_groups = sum(row['status'] in {'trial_verified', 'trial_unverified'}
                      for row in strategies.values())
    return {
        'version': 'iwencai-openapi-trial-reference-v1',
        'provider': 'iwencai_openapi', 'source_mode': 'openapi_trial',
        'status': ('pending' if awaiting_artifact else
                   'trial_verified' if verified_groups == len(ORDER) else
                   'trial_partially_verified' if verified_groups else
                   'trial_unverified' if data_groups else 'degraded'),
        'availability_reason': (
            'awaiting_daily_shadow_artifact' if awaiting_artifact else None
        ),
        'trial_reference': True,
        'semantic_equivalence_verified': verified_groups == len(ORDER),
        'ready_groups': verified_groups,
        'semantic_verified_groups': verified_groups,
        'trial_data_groups': data_groups,
        'executed_at': payload.get('sampled_at') or payload.get('executed_at'),
        'strategies': strategies,
        'reference_affects_membership': False,
    }



def latest_openapi_shadow(artifacts):
    return next(((artifacts.get(name) or {}).get("payload") for name in (
        "iwencai_openapi_shadow_postclose", "iwencai_openapi_shadow_afternoon",
        "iwencai_openapi_shadow_open_retry", "iwencai_openapi_shadow",
    ) if (artifacts.get(name) or {}).get("payload")), {})


def wencai_reference(artifacts, *, shadow=None):
    """Read the same configured reference for Web, Agent and scheduled reports."""
    if os.getenv('WENCAI_REFERENCE_SOURCE', 'legacy').strip().lower() != 'openapi_trial':
        from selection.strategy_cache import artifact_payload
        return artifact_payload(artifacts)
    shadow = latest_openapi_shadow(artifacts) if shadow is None else shadow
    symbols = [str(symbol) for row in (shadow.get('groups') or []) if isinstance(row, dict)
               for symbol in (row.get('picks') or [])
               if isinstance(symbol, str) and re.fullmatch(r'\d{6}', symbol)]
    cached_names = {}
    if symbols:
        try:
            from data import datahub
            cached_names = datahub.cached_stock_names(symbols)
        except Exception:
            pass
    return openapi_trial_reference(shadow, cached_names=cached_names)
