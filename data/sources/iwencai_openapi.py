"""Bounded A-share OpenAPI shadow client; never feeds formal selection.

The independent SkillHub key is read only from runtime environment or a
restricted file. No response body, key, header, or exception text is logged.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from zoneinfo import ZoneInfo

import requests

from data.provider_governor import (
    SourceBudgetUnavailable, provider_slot, set_provider_cooldown,
)
from selection.wencai_query_contract import ORDER, QUERIES


URL = 'https://openapi.iwencai.com/v1/query2data'
SKILL_ID = 'hithink-astock-selector'
PAGE_LIMIT = 20
MAX_PAGES = 2
MAX_RESPONSE_BYTES = 262144
_DIRECT_PROXIES = {'http': '', 'https': '', 'all': ''}
_DATE_FIELD = re.compile(r'\[(20\d{6})\]')
_CODE_FIELD = ('股票代码', '证券代码', '代码', 'stock_code', 'symbol')
_NAME_FIELD = ('股票简称', '证券简称', '名称', 'stock_name', 'name')
_REQUIRED_FIELDS = {
    '低价擒牛': (('收盘价', '最新价', '股价'), ('归母净利润同比增长率',), ('成交额',)),
    '低估值': (('市盈率ttm',), ('市净率',), ('股息率',), ('资产负债率',), ('流通市值',)),
    '主力资金': (('主力资金净流入', '主力资金流向'),),
    '小市值': (('总市值',), ('营业收入同比增长率',), ('归母净利润同比增长率',)),
    '净利增长': (('归母净利润同比增长率',), ('成交额',)),
}
_SORT_FIELDS = {
    '低价擒牛': (('成交额',), False),
    '低估值': (('流通市值',), False),
    '主力资金': (('主力资金净流入', '主力资金流向'), True),
    '小市值': (('总市值',), False),
    '净利增长': (('成交额',), False),
}
_TARGET_TOP_N = {'低价擒牛': 5, '低估值': 10, '主力资金': 5,
                 '小市值': 5, '净利增长': 5}
_FINANCIAL_GROUPS = frozenset(('低价擒牛', '低估值', '小市值', '净利增长'))
_CONDITION_RULES = {
    '低价擒牛': ((0, 'lt', 20.0, 'CNY'), (1, 'ge', 100.0, 'percent')),
    '低估值': ((0, 'le', 20.0, 'ratio'), (1, 'le', 1.5, 'ratio'),
             (2, 'ge', 1.0, 'percent'), (3, 'le', 30.0, 'percent')),
    '主力资金': (),
    '小市值': ((0, 'le', 5_000_000_000.0, 'CNY'),
             (1, 'ge', 10.0, 'percent'), (2, 'ge', 100.0, 'percent')),
    '净利增长': ((0, 'ge', 10.0, 'percent'),),
}
_REPORT_SOURCE_FIELDS = {
    '低价擒牛': ('净利润来源说明',),
    '小市值': ('营业收入来源说明', '净利润来源说明'),
    '净利增长': ('净利润来源说明',),
}
_REPORT_PERIOD = re.compile(r'(20\d{2})年(一季报|中报|三季报|年报)')


def configured_key() -> str:
    key = os.getenv('IWENCAI_API_KEY', '').strip()
    if key:
        return key
    filename = os.getenv('IWENCAI_API_KEY_FILE', '').strip()
    if not filename:
        return ''
    try:
        path = Path(filename)
        if path.stat().st_mode & 0o077:
            return ''
        content = path.read_text(encoding='utf-8')
        for line in content.splitlines():
            line = line.strip()
            if line.startswith('export '):
                line = line[7:].strip()
            if line.startswith('IWENCAI_API_KEY='):
                return line.partition('=')[2].strip().strip('"\'')
        return content.strip() if '\n' not in content and '=' not in content else ''
    except (OSError, UnicodeError):
        return ''


def _symbol(row: dict) -> str:
    value = next((row.get(key) for key in _CODE_FIELD if row.get(key)), '')
    code = str(value).strip().upper()
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', code)
    return match.group(1) if match else ''


def _stock_name(row: dict) -> str:
    value = next((row.get(key) for key in _NAME_FIELD if row.get(key)), '')
    name = str(value).strip()
    return name[:40] if name and not name.isdigit() else ''


def _field_candidates(row: dict, aliases: tuple[str, ...]) -> list[str]:
    """Never treat a ranking's ordinal/base as the underlying financial metric."""
    matched = [str(field) for field in row
               if any(alias.casefold() in str(field).casefold() for alias in aliases)
               and not any(word in str(field) for word in ('排名', '名次', '基数'))]
    exact = [field for field in matched
             if _DATE_FIELD.sub('', field).strip().casefold()
             in {alias.casefold() for alias in aliases}]
    return exact or matched


def _field_evidence(rows: list[dict], aliases: tuple[str, ...]) -> tuple[str | None, dict]:
    candidates = _field_candidates(rows[0], aliases)
    sample = rows[:20]
    details = []
    for field in candidates[:6]:
        match = _DATE_FIELD.search(field)
        numeric = [float(row[field]) for row in sample
                   if isinstance(row.get(field), (int, float))
                   and not isinstance(row.get(field), bool)]
        details.append({
            'field': field[:80],
            'numeric_rows': len(numeric),
            'sample_rows': len(sample),
            'as_of': match.group(1) if match else None,
            'numeric_min': min(numeric) if numeric else None,
            'numeric_max': max(numeric) if numeric else None,
        })
    full_numeric = [field for field in candidates[:6]
                    if all(isinstance(row.get(field), (int, float))
                           and not isinstance(row.get(field), bool) for row in sample)]
    selected = full_numeric[0] if len(full_numeric) == 1 else None
    return selected, {
        'aliases': list(aliases)[:3], 'candidates': details,
        'selected_field': selected,
        'ambiguous_numeric_fields': len(full_numeric) > 1,
    }


def _scope_rejections(name: str, row: dict) -> list[str]:
    code = str(next((row.get(key) for key in _CODE_FIELD if row.get(key)), '')).upper()
    stock_name = str(next((row.get(key) for key in _NAME_FIELD if row.get(key)), ''))
    symbol = _symbol(row)
    reasons = []
    if not symbol or not code.endswith(('.SH', '.SZ')):
        reasons.append('not_shenzhen_or_shanghai_a_share')
    if not stock_name:
        reasons.append('stock_name_missing')
    elif 'ST' in stock_name.upper():
        reasons.append('st_stock')
    if symbol.startswith(('688', '689')):
        reasons.append('star_market')
    if name != '主力资金' and symbol.startswith(('300', '301')):
        reasons.append('chinext')
    if name == '净利增长' and not code.endswith('.SZ'):
        reasons.append('not_shenzhen_a_share')
    return reasons


def _eligible_row(name: str, row: dict) -> bool:
    return not _scope_rejections(name, row)


def _threshold_checks(name: str, sample: list[dict],
                      selected_fields: list[str | None]) -> tuple[bool, list[dict]]:
    evidence = []
    verified = True
    operators = {
        'lt': lambda value, threshold: value < threshold,
        'le': lambda value, threshold: value <= threshold,
        'ge': lambda value, threshold: value >= threshold,
    }
    for field_index, operator, threshold, unit in _CONDITION_RULES[name]:
        field = selected_fields[field_index] if field_index < len(selected_fields) else None
        values = [row.get(field) for row in sample] if field else []
        numeric = [float(value) for value in values
                   if isinstance(value, (int, float)) and not isinstance(value, bool)]
        matched = sum(operators[operator](value, threshold) for value in numeric)
        passed = bool(sample and len(numeric) == len(sample) and matched == len(sample))
        verified &= passed
        evidence.append({
            'field': field, 'operator': operator, 'threshold': threshold, 'unit': unit,
            'sample_rows': len(sample), 'numeric_rows': len(numeric),
            'matched_rows': matched, 'verified': passed,
        })
    return verified, evidence


def _report_period(value) -> str | None:
    match = _REPORT_PERIOD.search(str(value or ''))
    if not match:
        return None
    suffix = {'一季报': '0331', '中报': '0630', '三季报': '0930', '年报': '1231'}
    return match.group(1) + suffix[match.group(2)]


def _financial_period_checks(name: str, sample: list[dict],
                             field_evidence: list[dict]) -> tuple[bool, list[dict]]:
    if name not in _FINANCIAL_GROUPS:
        return True, []
    evidence = []
    if name == '低估值':
        for item in field_evidence:
            field = str(item.get('selected_field') or '')
            match = _DATE_FIELD.search(field)
            latest = '最新' in field
            passed = bool(field and (match or latest))
            evidence.append({
                'field': field or None,
                'basis': match.group(1) if match else 'latest' if latest else None,
                'verified': passed,
            })
        return bool(evidence and all(item['verified'] for item in evidence)), evidence
    for field in _REPORT_SOURCE_FIELDS.get(name, ()):
        periods = [_report_period(row.get(field)) for row in sample]
        unique = sorted({period for period in periods if period})
        passed = bool(sample and len(unique) == 1 and all(periods))
        evidence.append({'source_field': field, 'periods': unique[:3], 'verified': passed})
    if name == '小市值' and len(evidence) == 2:
        same_period = evidence[0]['periods'] == evidence[1]['periods']
        evidence.append({'source_field': '收入与净利润报告期',
                         'periods': evidence[0]['periods'] if same_period else [],
                         'verified': same_period})
    return bool(evidence and all(item['verified'] for item in evidence)), evidence


def _semantic_checks(name: str, rows: list[dict], query: str) -> dict:
    if not rows:
        return {'required_numeric_fields': False, 'sort_verified': False,
                'stock_scope_verified': False, 'sort_field_as_of': None,
                'field_evidence': [], 'financial_periods_verified': False,
                'financial_period_evidence': [], 'capital_flow_metric_verified': False,
                'threshold_conditions_verified': False, 'condition_evidence': [],
                'query_contract_verified': False, 'as_of_verified': False,
                'local_conditions_verified': False, 'eligible_top_n_count': 0}
    sample = rows[:20]
    evidence = [_field_evidence(sample, aliases) for aliases in _REQUIRED_FIELDS[name]]
    selected_fields = [field for field, _ in evidence]
    field_evidence = [item for _, item in evidence]
    required = all(field is not None for field in selected_fields)
    aliases, descending = _SORT_FIELDS[name]
    sort_field, sort_evidence = _field_evidence(sample, aliases)
    values = [row.get(sort_field) for row in sample] if sort_field else []
    sort_verified = bool(values and all(isinstance(value, (int, float)) for value in values)
                         and all((left >= right if descending else left <= right)
                                 for left, right in zip(values, values[1:])))
    rejections = [_scope_rejections(name, row) for row in sample]
    eligible = [not reasons for reasons in rejections]
    target = _TARGET_TOP_N[name]
    scope_verified = bool(len(eligible) >= target and all(eligible))
    date_match = _DATE_FIELD.search(sort_field or '')
    financial_periods, financial_evidence = _financial_period_checks(
        name, sample, field_evidence,
    )
    threshold_conditions, condition_evidence = _threshold_checks(
        name, sample, selected_fields,
    )
    query_contract = query == QUERIES[name] and query.count('，') + 1 <= 15
    capital_flow_metric = bool(
        name != '主力资金' or
        (sort_field and any(term in sort_field for term in ('主力资金净流入', '主力资金流向'))
         and date_match and '主力资金净流入额' in query)
    )
    as_of_verified = bool(date_match)
    local_conditions = bool(
        query_contract and required and threshold_conditions and scope_verified
        and financial_periods and capital_flow_metric and as_of_verified
    )
    rejection_counts: dict[str, int] = {}
    for reasons in rejections:
        for reason in reasons:
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
    return {
        'required_numeric_fields': required, 'sort_verified': sort_verified,
        'stock_scope_verified': scope_verified,
        'sort_field_as_of': date_match.group(1) if date_match else None,
        'field_evidence': field_evidence,
        'sort_evidence': sort_evidence,
        'threshold_conditions_verified': threshold_conditions,
        'condition_evidence': condition_evidence,
        'query_contract_verified': query_contract,
        'as_of_verified': as_of_verified,
        'financial_periods_verified': financial_periods,
        'financial_period_evidence': financial_evidence,
        'capital_flow_metric_verified': capital_flow_metric,
        'local_conditions_verified': local_conditions,
        'eligible_top_n_count': min(target, sum(eligible)),
        'scope_rejected_sample_count': len(eligible) - sum(eligible),
        'scope_rejection_counts': rejection_counts,
    }


def _one_page(query: str, page: int, key: str, *, session=None) -> dict:
    own_session = session is None
    client = session or requests.Session()
    client.trust_env = False  # NAS direct path, including injected sessions.
    try:
        body = b''
        retry_after = 120
        with provider_slot('iwencai_openapi'):
            with client.post(
                URL,
                json={'query': query, 'page': str(page), 'limit': str(PAGE_LIMIT),
                      'is_cache': '1', 'expand_index': 'true'},
                headers={
                    'Authorization': 'Bearer ' + key,
                    'Content-Type': 'application/json',
                    'X-Claw-Call-Type': 'normal',
                    'X-Claw-Skill-Id': SKILL_ID,
                    'X-Claw-Skill-Version': '1.0.0',
                    'X-Claw-Plugin-Id': 'none',
                    'X-Claw-Plugin-Version': 'none',
                    'X-Claw-Trace-Id': secrets.token_hex(32),
                },
                timeout=(3, 8), stream=True, proxies=dict(_DIRECT_PROXIES),
            ) as response:
                status = response.status_code
                if status == 429:
                    try:
                        retry_after = max(120, min(3600, int(response.headers.get('Retry-After', 120))))
                    except (TypeError, ValueError):
                        pass
                if status == 200:
                    chunks = []
                    size = 0
                    for chunk in response.iter_content(chunk_size=8192):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            return {'status': 'response_too_large', 'http_status': status}
                        chunks.append(chunk)
                    body = b''.join(chunks)
        if status in (401, 403, 429):
            set_provider_cooldown('iwencai_openapi',
                                  3600 if status == 401 else 900 if status == 403 else retry_after)
        if status != 200:
            return {'status': 'http_' + str(status), 'http_status': status}
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return {'status': 'invalid_json', 'http_status': status}
        if not isinstance(payload, dict) or not isinstance(payload.get('datas'), list):
            if isinstance(payload, dict) and str(payload.get('code') or '') == '429':
                set_provider_cooldown('iwencai_openapi', 120)
                return {'status': 'quota_rejected', 'http_status': status}
            return {'status': 'invalid_schema', 'http_status': status}
        try:
            total = int(payload.get('code_count'))
        except (TypeError, ValueError):
            return {'status': 'invalid_schema', 'http_status': status}
        if total < 0 or any(not isinstance(row, dict) for row in payload['datas']):
            return {'status': 'invalid_schema', 'http_status': status}
        chunks_info = payload.get('chunks_info')
        return {
            'status': 'ok', 'http_status': status,
            'datas': payload['datas'], 'code_count': total,
            'chunks_info_present': isinstance(chunks_info, dict),
            'parsed_conditions_present': bool(
                isinstance(chunks_info, dict)
                and chunks_info.get('query') == query
                and isinstance(chunks_info.get('parsed_conditions'), list)
                and chunks_info['parsed_conditions']
            ),
            # Merely echoing a few parsed conditions does not prove that every
            # comparison, universe and sort clause matched the original query.
            'parsed_conditions_verified': False,
        }
    except requests.Timeout:
        return {'status': 'timeout', 'http_status': None}
    except requests.RequestException:
        return {'status': 'transport_error', 'http_status': None}
    except SourceBudgetUnavailable:
        return {'status': 'budget_or_cooldown', 'http_status': None}
    except Exception:
        return {'status': 'provider_guard_error', 'http_status': None}
    finally:
        if own_session:
            client.close()


def run_group(name: str, *, session=None, key: str | None = None) -> dict:
    """At most two pages and one result per group; errors never cross groups."""
    if name not in ORDER:
        raise ValueError('unknown_wencai_group')
    now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    query = QUERIES[name]
    base = {
        'name': name, 'provider': 'iwencai_openapi', 'reference_only': True,
        'query_hash': hashlib.sha256(query.encode()).hexdigest(),
        'requested_at': now, 'data_as_of': None, 'as_of_verified': False,
        'pages_fetched': 0,
        'reported_count': None, 'returned_count': 0, 'picks': [],
        'schema_valid': False, 'chunks_info_present': False,
        'local_conditions_verified': False,
        'condition_count': query.count('，') + 1,
        'max_conditions_ok': query.count('，') + 1 <= 15,
    }
    token = configured_key() if key is None else key
    if not token:
        return base | {'status': 'credential_missing'}
    rows: list[dict] = []
    total = None
    all_chunks_present = True
    all_parsed_present = True
    all_conditions_parsed = True
    for page in range(1, MAX_PAGES + 1):
        result = _one_page(query, page, token, session=session)
        base['pages_fetched'] = page
        base['http_status'] = result.get('http_status')
        if result['status'] != 'ok':
            return base | {'status': result['status']}
        if total is not None and total != result['code_count']:
            return base | {'status': 'inconsistent_count'}
        total = result['code_count']
        all_chunks_present &= result['chunks_info_present']
        all_parsed_present &= result['parsed_conditions_present']
        all_conditions_parsed &= result['parsed_conditions_verified']
        page_rows = result['datas']
        if total and not page_rows:
            return base | {'status': 'incomplete_page'}
        rows.extend(page_rows)
        if len(rows) >= total:
            break
    symbols = []
    for row in rows:
        symbol = _symbol(row)
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    dates = [m.group(1) for row in rows[:20] for field in row
             for m in [_DATE_FIELD.search(str(field))] if m]
    checks = _semantic_checks(name, rows, query)
    target = _TARGET_TOP_N[name]
    selected_rows = [row for row in rows if _eligible_row(name, row)]
    top_n_coverage = len(selected_rows) >= min(total or 0, target)
    ranking_verified = bool(
        checks['local_conditions_verified'] and checks['sort_verified']
        and top_n_coverage
    )
    semantic_verified = ranking_verified
    checks.update(
        ranking_verified=ranking_verified,
        semantic_verified=semantic_verified,
        verification_stage=(
            'ranking_verified' if ranking_verified else
            'conditions_verified' if checks['local_conditions_verified'] else
            'response_received'
        ),
    )
    reasons = []
    if not checks['query_contract_verified']:
        reasons.append('query_contract_unverified')
    if not checks['as_of_verified']:
        reasons.append('as_of_not_verified')
    if not checks['required_numeric_fields']:
        reasons.append('required_metric_missing_or_ambiguous')
    if not checks['threshold_conditions_verified']:
        reasons.append('threshold_conditions_unverified')
    if not checks['sort_verified']:
        reasons.append('sort_metric_unverified')
    if not checks['stock_scope_verified']:
        reasons.append('stock_scope_unverified')
    if not checks['sort_field_as_of']:
        reasons.append('sort_metric_date_missing')
    if not checks['financial_periods_verified']:
        reasons.append('financial_period_missing')
    if not checks['capital_flow_metric_verified']:
        reasons.append('capital_flow_net_inflow_unverified')
    if not top_n_coverage:
        reasons.append('top_n_coverage_insufficient')
    audit_warnings = []
    if not all_conditions_parsed:
        audit_warnings.append('provider_condition_parse_receipt_unavailable')
    if len(rows) < (total or 0):
        audit_warnings.append('full_result_pagination_not_fetched_top_n_only')
    pick_details = []
    seen_picks = set()
    for row in selected_rows:
        symbol = _symbol(row)
        if not symbol or symbol in seen_picks:
            continue
        seen_picks.add(symbol)
        pick_details.append({'symbol': symbol, 'name': _stock_name(row)})
        if len(pick_details) >= 5:
            break
    base.update(
        status='complete' if semantic_verified else 'semantic_unverified',
        schema_valid=bool(rows == [] and total == 0 or symbols),
        data_as_of=checks['sort_field_as_of'] or (max(dates) if dates else None),
        reported_count=total, returned_count=len(rows),
        picks=[row['symbol'] for row in pick_details],
        pick_details=pick_details,
        target_top_n=target, top_n_coverage=top_n_coverage,
        failure_reasons=reasons,
        audit_warnings=audit_warnings,
        pagination_complete=len(rows) >= (total or 0),
        chunks_info_present=all_chunks_present,
        parsed_conditions_present=all_parsed_present,
        parsed_conditions_verified=all_conditions_parsed,
        **checks,
    )
    if total == 0:
        base['status'] = 'empty_result'
    elif not base['schema_valid']:
        base['status'] = 'invalid_stock_schema'
    elif rows and name == '主力资金' and not checks['required_numeric_fields']:
        base['status'] = 'entitlement_unavailable'
    return base


def run_shadow(old_reference: dict | None = None, *, session=None,
               key: str | None = None, group_runner=None) -> dict:
    """Read-only five-group evaluation; no candidate ranking or trading side effects."""
    old = (old_reference or {}).get('strategies') or {}
    groups = []
    for name in ORDER:
        row = (group_runner(name) if group_runner is not None else
               run_group(name, session=session, key=key))
        old_row = old.get(name) or {}
        old_symbols = {_symbol(p) for p in (old_row.get('picks') or []) if isinstance(p, dict)}
        new_symbols = set(row.get('picks') or [])
        comparable = old_row.get('status') == 'ready' and row['status'] == 'complete'
        observed = old_row.get('status') == 'ready' and bool(new_symbols)
        row['legacy_status'] = str(old_row.get('status') or 'missing')[:32]
        row['overlap_top5_count'] = len(old_symbols & new_symbols) if comparable else None
        row['observed_overlap_top5_count'] = len(old_symbols & new_symbols) if observed else None
        row['comparison_available'] = comparable
        groups.append(row)
    trial_active = os.getenv('WENCAI_REFERENCE_SOURCE', 'legacy').strip().lower() == 'openapi_trial'
    semantic_verified_groups = sum(bool(group.get('semantic_verified')) for group in groups)
    all_semantic_verified = len(groups) == len(ORDER) and semantic_verified_groups == len(ORDER)
    return {
        'version': 'iwencai-openapi-shadow-v1',
        'provider': 'iwencai_openapi', 'skill_id': SKILL_ID,
        'status': ('credential_missing' if all(g['status'] == 'credential_missing' for g in groups)
                   else 'complete' if all(g['status'] == 'complete' for g in groups)
                   else 'degraded'),
        'executed_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'groups': groups, 'ready_groups': sum(g['status'] == 'complete' for g in groups),
        'semantic_verified_groups': semantic_verified_groups,
        'data_groups': sum(bool(g.get('schema_valid') and g.get('returned_count')) for g in groups),
        'reference_only': True, 'reference_affects_membership': False,
        'replacement_ready': all_semantic_verified,
        'reference_mode': 'openapi_trial' if trial_active else 'shadow_validation',
        'trial_authorized': trial_active,
        'replacement_status': (
            'trial_active_semantic_verified' if trial_active and all_semantic_verified else
            'trial_active_semantic_unverified' if trial_active else
            'semantic_validated_reference_ready' if all_semantic_verified else
            'entitlement_or_semantic_blocked'
        ),
        'valid_semantic_sample_day': all_semantic_verified,
        'required_user_options': [] if trial_active else [
            'upgrade_entitlement', 'retain_legacy_reference',
            'explicitly_authorize_strategy_redefinition',
        ],
        'replacement_gates': ([] if all_semantic_verified else [
            'query_conditions_verified', 'field_entitlement_verified',
            'current_data_as_of', 'known_cost_and_limits',
        ] if trial_active else [
            'valid_credentials', 'all_five_groups_complete', 'current_data_as_of',
            'two_full_trading_days', 'query_conditions_verified',
            'field_entitlement_verified', 'known_cost_and_limits',
        ]),
    }
