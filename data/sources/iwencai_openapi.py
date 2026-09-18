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
    '低价擒牛': (('最新价', '股价'), ('净利润同比增长率', '归母净利润同比增长率'), ('成交额',)),
    '低估值': (('市盈率',), ('市净率',), ('股息率',), ('资产负债率',), ('流通市值',)),
    '主力资金': (('主力资金净流入', '主力资金流向'),),
    '小市值': (('总市值',), ('营收增长率', '营业收入同比增长率'),
              ('净利润同比增长率', '归母净利润同比增长率')),
    '净利增长': (('净利润同比增长率', '归母净利润同比增长率'), ('成交额',)),
}
_SORT_FIELDS = {
    '低价擒牛': (('成交额',), False),
    '低估值': (('流通市值',), False),
    '主力资金': (('主力资金净流入', '主力资金流向'), True),
    '小市值': (('总市值',), False),
    '净利增长': (('成交额',), False),
}


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


def _field(row: dict, aliases: tuple[str, ...]) -> str | None:
    return next((str(field) for field in row if any(alias in str(field) for alias in aliases)), None)


def _semantic_checks(name: str, rows: list[dict]) -> dict:
    if not rows:
        return {'required_numeric_fields': False, 'sort_verified': False,
                'stock_scope_verified': False, 'sort_field_as_of': None}
    sample = rows[:20]
    required = all(
        (field := _field(sample[0], aliases)) is not None
        and all(isinstance(row.get(field), (int, float)) for row in sample)
        for aliases in _REQUIRED_FIELDS[name]
    )
    aliases, descending = _SORT_FIELDS[name]
    sort_field = _field(sample[0], aliases)
    values = [row.get(sort_field) for row in sample] if sort_field else []
    sort_verified = bool(values and all(isinstance(value, (int, float)) for value in values)
                         and all((left >= right if descending else left <= right)
                                 for left, right in zip(values, values[1:])))
    symbols = [_symbol(row) for row in sample]
    codes = [str(next((row.get(key) for key in _CODE_FIELD if row.get(key)), '')).upper()
             for row in sample]
    names = [str(next((row.get(key) for key in _NAME_FIELD if row.get(key)), ''))
             for row in sample]
    scope_verified = bool(
        all(symbols) and all(names)
        and all(code.endswith(('.SH', '.SZ')) for code in codes)
        and all('ST' not in stock_name.upper() for stock_name in names)
        and all(not symbol.startswith(('688', '689')) for symbol in symbols)
        and (name == '主力资金' or all(
            not symbol.startswith(('300', '301')) for symbol in symbols))
        and (name != '净利增长' or all(code.endswith('.SZ') for code in codes))
    )
    date_match = _DATE_FIELD.search(sort_field or '')
    return {
        'required_numeric_fields': required, 'sort_verified': sort_verified,
        'stock_scope_verified': scope_verified,
        'sort_field_as_of': date_match.group(1) if date_match else None,
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
            'parsed_conditions_verified': bool(
                isinstance(chunks_info, dict)
                and chunks_info.get('query') == query
                and isinstance(chunks_info.get('parsed_conditions'), list)
                and chunks_info['parsed_conditions']
            ),
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
        'requested_at': now, 'data_as_of': None, 'pages_fetched': 0,
        'reported_count': None, 'returned_count': 0, 'picks': [],
        'schema_valid': False, 'chunks_info_present': False,
        'condition_count': query.count('，') + 1,
        'max_conditions_ok': query.count('，') + 1 <= 15,
    }
    token = configured_key() if key is None else key
    if not token:
        return base | {'status': 'credential_missing'}
    rows: list[dict] = []
    total = None
    all_chunks_present = True
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
    checks = _semantic_checks(name, rows)
    base.update(
        status='complete' if len(rows) >= (total or 0) else 'partial',
        schema_valid=bool(rows == [] and total == 0 or symbols),
        data_as_of=max(dates) if dates else None,
        reported_count=total, returned_count=len(rows),
        picks=symbols[:5],
        pagination_complete=len(rows) >= (total or 0),
        chunks_info_present=all_chunks_present,
        parsed_conditions_verified=all_conditions_parsed,
        **checks,
    )
    if total == 0:
        base['status'] = 'empty_result'
    elif not base['schema_valid']:
        base['status'] = 'invalid_stock_schema'
    elif rows and name == '主力资金' and not checks['required_numeric_fields']:
        base['status'] = 'entitlement_unavailable'
    elif rows and not (checks['required_numeric_fields'] and checks['sort_verified']
                       and checks['stock_scope_verified'] and checks['sort_field_as_of']
                       and base['parsed_conditions_verified']):
        base['status'] = 'semantic_unverified'
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
    return {
        'version': 'iwencai-openapi-shadow-v1',
        'provider': 'iwencai_openapi', 'skill_id': SKILL_ID,
        'status': ('credential_missing' if all(g['status'] == 'credential_missing' for g in groups)
                   else 'complete' if all(g['status'] == 'complete' for g in groups)
                   else 'degraded'),
        'executed_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
        'groups': groups, 'ready_groups': sum(g['status'] == 'complete' for g in groups),
        'data_groups': sum(bool(g.get('schema_valid') and g.get('returned_count')) for g in groups),
        'reference_only': True, 'reference_affects_membership': False,
        'replacement_ready': False,
        'replacement_gates': [
            'valid_credentials', 'all_five_groups_complete', 'current_data_as_of',
            'two_full_trading_days', 'query_conditions_verified',
            'field_entitlement_verified', 'known_cost_and_limits',
        ],
    }
