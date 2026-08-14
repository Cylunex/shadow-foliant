"""面向 Agent/MCP 的低轮次成交导入。

保留底层 ``portfolio_db.import_trades`` 的完整参数能力，同时允许只给股票名称、
成交时间、价格、数量和方向，自动补代码与成交额；也可直接粘贴 Markdown 表格。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo


_BUY = {'买入', '买', 'buy', 'b', '申购', '加仓'}
_SELL = {'卖出', '卖', 'sell', 's', '赎回', '减仓'}


def _clean_text(value: Any) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^\*\*(.*?)\*\*$', r'\1', text)
    return text.strip()


def _number(value: Any) -> Optional[float]:
    if value in (None, ''):
        return None
    text = str(value).replace(',', '').replace('¥', '').replace('￥', '').replace('元', '').strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _code(value: Any) -> str:
    text = _clean_text(value).upper()
    if not text:
        return ''
    match = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    if match:
        return match.group(1)
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return ''


def _trade_type(value: Any) -> str:
    raw = _clean_text(value)
    if raw in _BUY or raw.lower() in _BUY:
        return '买入'
    if raw in _SELL or raw.lower() in _SELL:
        return '卖出'
    return ''


def _time_key(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ''
    normalized = text.replace('/', '-').replace('T', ' ')
    try:
        dt = datetime.fromisoformat(normalized.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo('Asia/Shanghai'))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return normalized[:19]


def parse_markdown_table(table: str) -> List[Dict[str, str]]:
    """解析常见 Markdown 管道表；非表格文本返回空列表。"""
    lines = [line.strip() for line in str(table or '').splitlines() if line.strip().startswith('|')]
    if len(lines) < 2:
        return []

    def cells(line: str) -> List[str]:
        return [_clean_text(cell) for cell in line.strip().strip('|').split('|')]

    headers = cells(lines[0])
    if not headers or all(not h for h in headers):
        return []
    out: List[Dict[str, str]] = []
    for line in lines[1:]:
        values = cells(line)
        if values and all(re.fullmatch(r':?-{3,}:?', value.replace(' ', '')) for value in values):
            continue
        if not any(values):
            continue
        if len(values) < len(headers):
            values += [''] * (len(headers) - len(values))
        out.append(dict(zip(headers, values[:len(headers)])))
    return out


def _pick(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if row.get(key) not in (None, ''):
            return row.get(key)
    return None


def _known_names(portfolio_db) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    by_name: Dict[str, List[str]] = {}
    by_code: Dict[str, str] = {}
    try:
        rows: Iterable[Dict[str, Any]] = ((portfolio_db.get_all_stocks() or [])
                                           + (portfolio_db.get_trades(None, 10000) or []))
    except Exception:
        rows = []
    for row in rows:
        name = _clean_text(row.get('name') or row.get('stock_name'))
        code = _code(row.get('code') or row.get('stock_code'))
        if not name or not code:
            continue
        if code not in by_name.setdefault(name, []):
            by_name[name].append(code)
        by_code.setdefault(code, name)
    return by_name, by_code


def _dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str, int, float]:
    return (_code(row.get('code') or row.get('stock_code')),
            _time_key(row.get('trade_time') or row.get('成交时间')),
            _trade_type(row.get('trade_type') or row.get('交易类型') or row.get('方向')),
            int(_number(row.get('quantity') or row.get('成交量') or row.get('数量')) or 0),
            round(float(_number(row.get('price') or row.get('成交价') or row.get('价格')) or 0), 4))


def prepare_trades(rows: Optional[List[Dict[str, Any]]] = None, table: str = '',
                   portfolio_db=None, allow_name_refresh: bool = True) -> Dict[str, Any]:
    """标准化并补齐成交，不写库。任一行关键字段有误时仍返回其他预览，但由上层整批拦截。"""
    source_rows: List[Dict[str, Any]] = []
    if isinstance(rows, dict):
        source_rows.append(rows)
    elif isinstance(rows, list):
        source_rows.extend(row for row in rows if isinstance(row, dict))
    if table:
        parsed = parse_markdown_table(table)
        if not parsed:
            return {'received': len(source_rows), 'rows': [], 'errors': ['无法解析 table Markdown 表格'],
                    'warnings': [], 'unresolved': [], 'resolved_codes': {}}
        source_rows.extend(parsed)

    if portfolio_db is None:
        from portfolio_db import portfolio_db as _portfolio_db
        portfolio_db = _portfolio_db
    known_by_name, known_by_code = _known_names(portfolio_db)

    staged: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []
    missing_names: List[str] = []
    for idx, row in enumerate(source_rows, 1):
        name = _clean_text(_pick(row, 'name', '股票名称', '股票简称', '证券名称'))
        code = _code(_pick(row, 'code', '股票代码', 'symbol', '证券代码'))
        price = _number(_pick(row, 'price', '成交价', '价格'))
        qty_num = _number(_pick(row, 'quantity', '成交量', '数量', 'qty'))
        ttype = _trade_type(_pick(row, 'trade_type', '交易类型', '方向', 'direction'))
        trade_time = _clean_text(_pick(row, 'trade_time', '成交时间', '日期', 'date'))
        if not name and not code:
            errors.append(f'第 {idx} 行缺少股票名称或代码')
        if price is None or price <= 0:
            errors.append(f'第 {idx} 行成交价无效')
        if qty_num is None or qty_num <= 0 or not float(qty_num).is_integer():
            errors.append(f'第 {idx} 行成交量必须是正整数')
        if not ttype:
            errors.append(f'第 {idx} 行交易类型必须是买入或卖出')
        if (not name and not code) or price is None or price <= 0 or qty_num is None \
                or qty_num <= 0 or not float(qty_num).is_integer() or not ttype:
            continue
        if not code:
            local_codes = known_by_name.get(name, [])
            if len(local_codes) == 1:
                code = local_codes[0]
            else:
                missing_names.append(name)
        staged.append({
            '_row': idx, 'code': code, 'name': name, 'trade_type': ttype,
            'quantity': int(qty_num), 'price': round(float(price), 4),
            'trade_time': trade_time or None, '_input': row,
        })

    resolved_external: Dict[str, str] = {}
    unresolved_names = sorted(set(name for name in missing_names if name))
    if unresolved_names:
        try:
            import datahub
            resolved_external = datahub.stock_codes(unresolved_names, allow_refresh=allow_name_refresh)
        except Exception:
            resolved_external = {}

    resolved_codes: Dict[str, str] = {}
    prepared: List[Dict[str, Any]] = []
    unresolved: List[str] = []
    for item in staged:
        if not item['code'] and item['name']:
            item['code'] = resolved_external.get(item['name'], '')
        if not item['code']:
            unresolved.append(item['name'] or f"第 {item['_row']} 行")
            continue
        if not item['name']:
            item['name'] = known_by_code.get(item['code'], '')
            if not item['name']:
                try:
                    import datahub
                    item['name'] = datahub.stock_name(item['code']) or item['code']
                except Exception:
                    item['name'] = item['code']
        resolved_codes[item['name']] = item['code']

        expected_amount = round(item['quantity'] * item['price'], 4)
        given_amount = _number(_pick(item['_input'], 'amount', '成交额', '金额'))
        if given_amount is not None and abs(given_amount - expected_amount) > 0.05:
            warnings.append(f"第 {item['_row']} 行成交额 {given_amount:g} 与价×量 {expected_amount:g} 不符，已按价×量补正")
        item['amount'] = expected_amount
        for target, aliases in {
            'note': ('note', '备注'), 'commission': ('commission', '佣金'),
            'tax': ('tax', '印花税'),
        }.items():
            value = _pick(item['_input'], *aliases)
            if value not in (None, ''):
                item[target] = value
        item['source'] = _pick(item['_input'], 'source') or 'mcp:import_trades'
        item.pop('_input', None)
        item.pop('_row', None)
        prepared.append(item)

    if unresolved:
        errors.append('以下股票名称无法唯一解析代码: ' + '、'.join(sorted(set(unresolved))))
    return {
        'received': len(source_rows), 'rows': prepared, 'errors': errors,
        'warnings': warnings, 'unresolved': sorted(set(unresolved)),
        'resolved_codes': resolved_codes,
    }


def import_trade_records(rows: Optional[List[Dict[str, Any]]] = None, table: str = '',
                         update_position: bool = True, dry_run: bool = False,
                         skip_existing: bool = True, portfolio_db=None,
                         allow_name_refresh: bool = True) -> Dict[str, Any]:
    """高层成交导入；默认幂等，关键字段错误/名称未解析时整批不写。"""
    if portfolio_db is None:
        from portfolio_db import portfolio_db as _portfolio_db
        portfolio_db = _portfolio_db
    prepared = prepare_trades(rows, table, portfolio_db=portfolio_db,
                              allow_name_refresh=allow_name_refresh)
    result: Dict[str, Any] = {
        'status': 'ready', 'received': prepared['received'], 'prepared': len(prepared['rows']),
        'imported': 0, 'failed': len(prepared['errors']), 'positions_updated': 0,
        'skipped_existing': 0, 'resolved_codes': prepared['resolved_codes'],
        'unresolved': prepared['unresolved'], 'warnings': prepared['warnings'],
        'errors': prepared['errors'],
    }
    if prepared['errors']:
        result['status'] = 'needs_input'
        result['preview'] = prepared['rows']
        return result

    rows_to_import = list(prepared['rows'])
    if skip_existing and rows_to_import:
        try:
            existing = {_dedupe_key(row) for row in (portfolio_db.get_trades(None, 10000) or [])}
        except Exception:
            existing = set()
        unique_rows = []
        for row in rows_to_import:
            if _dedupe_key(row) in existing:
                result['skipped_existing'] += 1
            else:
                unique_rows.append(row)
                existing.add(_dedupe_key(row))  # 同一批内也去重
        rows_to_import = unique_rows
        result['prepared'] = len(rows_to_import)

    if dry_run:
        result['status'] = 'preview'
        result['preview'] = rows_to_import
        return result
    if not rows_to_import:
        result['status'] = 'noop'
        return result

    imported = portfolio_db.import_trades(rows_to_import, update_position=update_position) or {}
    result.update({key: imported.get(key, result.get(key))
                   for key in ('imported', 'failed', 'positions_updated')})
    if imported.get('errors'):
        result['errors'] = list(result['errors']) + list(imported['errors'])
    result['status'] = 'success' if not result['failed'] else 'partial'
    return result
