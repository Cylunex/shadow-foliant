"""面向 Agent/MCP 的低轮次成交导入。

保留底层 ``portfolio_db.import_trades`` 的完整参数能力，同时允许只给股票名称、
成交时间、价格、数量和方向，自动补代码与成交额；也可直接粘贴 Markdown 表格。
"""

from __future__ import annotations

import re
import math
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple


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
        value = float(text)
        return value if math.isfinite(value) else None
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
        # 兼容历史 PG 口径：用户输入的北京时间被 ::timestamptz 按数据库会话时区落库，
        # 但列表/API 一直展示原始时分秒；幂等键也必须比较“墙上时间”，不能二次换区。
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


def trade_execution_key(row: Dict[str, Any]) -> str:
    """Return a stable fill identity without depending on a recent-row scan.

    Broker execution identifiers are authoritative.  The fallback intentionally includes order,
    account, fee and source fields so two genuinely distinct equal-looking fills are not collapsed
    merely because their five display columns match.
    """
    source = _clean_text(row.get('source') or 'unknown').lower()
    account_ref = _clean_text(
        _pick(row, 'account_ref', 'account_id', '资金账号', '账户')
    )
    execution_id = _clean_text(
        _pick(row, 'broker_execution_id', 'execution_id', '成交编号', '成交序号')
    )
    if execution_id:
        return f"exec:{source}:{account_ref}:{execution_id}"
    payload = {
        'source': source,
        'account_ref': account_ref,
        'broker_order_id': _clean_text(
            _pick(row, 'broker_order_id', 'order_id', '委托编号', '合同编号')
        ),
        'code': _code(row.get('code') or row.get('stock_code')),
        'trade_time': _time_key(row.get('trade_time') or row.get('成交时间')),
        'trade_type': _trade_type(
            row.get('trade_type') or row.get('交易类型') or row.get('方向')
        ),
        'quantity': int(_number(row.get('quantity') or row.get('成交量') or row.get('数量')) or 0),
        'price': round(float(_number(row.get('price') or row.get('成交价') or row.get('价格')) or 0), 4),
        'commission': round(float(_number(row.get('commission') or row.get('佣金')) or 0), 4),
        'tax': round(float(_number(row.get('tax') or row.get('印花税')) or 0), 4),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'fp:' + hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _legacy_dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str, int, float]:
    return (_code(row.get('code') or row.get('stock_code')),
            _time_key(row.get('trade_time') or row.get('成交时间')),
            _trade_type(row.get('trade_type') or row.get('交易类型') or row.get('方向')),
            int(_number(row.get('quantity') or row.get('成交量') or row.get('数量')) or 0),
            round(float(_number(row.get('price') or row.get('成交价') or row.get('价格')) or 0), 4))


def _position_snapshot(portfolio_db) -> Dict[str, Dict[str, Any]]:
    try:
        rows = portfolio_db.get_all_stocks() or []
    except Exception:
        rows = []
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = _code(row.get('code') or row.get('stock_code'))
        if not code:
            continue
        out[code] = {
            'quantity': int(_number(row.get('quantity')) or 0),
            'cost_price': _number(row.get('cost_price')),
        }
    return out


def preview_position_effects(rows: List[Dict[str, Any]], portfolio_db,
                             *, update_position: bool = True) -> Dict[str, Any]:
    """Simulate a batch against one holdings snapshot and return a stable watermark."""
    positions = _position_snapshot(portfolio_db)
    affected = sorted({_code(row.get('code')) for row in rows if _code(row.get('code'))})
    initial = {code: dict(positions.get(code) or {'quantity': 0, 'cost_price': None})
               for code in affected}
    effects: List[Dict[str, Any]] = []
    errors: List[str] = []
    ordered = sorted(enumerate(rows), key=lambda item: (_time_key(item[1].get('trade_time')), item[0]))
    for original_index, row in ordered:
        code = _code(row.get('code'))
        before = dict(positions.get(code) or {'quantity': 0, 'cost_price': None})
        qty = int(_number(row.get('quantity')) or 0)
        price = float(_number(row.get('price')) or 0)
        side = _trade_type(row.get('trade_type'))
        after = dict(before)
        row_valid = True
        if update_position and side == '卖出':
            if before['quantity'] <= 0:
                errors.append(f"第 {original_index + 1} 行卖出失败：当前没有可用持仓")
                row_valid = False
            elif qty > before['quantity']:
                errors.append(
                    f"第 {original_index + 1} 行卖出失败：数量 {qty} 超过可用持仓 {before['quantity']}"
                )
                row_valid = False
            else:
                after['quantity'] = before['quantity'] - qty
        elif update_position and side == '买入':
            new_quantity = before['quantity'] + qty
            old_cost = before.get('cost_price')
            after['quantity'] = new_quantity
            after['cost_price'] = round(
                ((before['quantity'] * float(old_cost)) + qty * price) / new_quantity, 4
            ) if before['quantity'] and old_cost is not None else price
        if row_valid:
            positions[code] = after
        commission = float(_number(row.get('commission')) or 0)
        tax = float(_number(row.get('tax')) or 0)
        gross = round(qty * price, 4)
        cash_effect = round(-(gross + commission + tax), 4) if side == '买入' \
            else round(gross - commission - tax, 4)
        effects.append({
            'row': original_index + 1,
            'code': code,
            'position_before': before['quantity'],
            'position_after': after['quantity'] if update_position else before['quantity'],
            'gross_amount': gross,
            'fees': round(commission + tax, 4),
            'net_cash_effect': cash_effect,
            'position_effect': 'apply' if update_position else 'record_only',
        })
    watermark_payload = {
        code: {'quantity': value['quantity'], 'cost_price': value.get('cost_price')}
        for code, value in sorted(initial.items())
    }
    watermark = hashlib.sha256(
        json.dumps(watermark_payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return {'effects': effects, 'errors': errors, 'position_watermark': watermark}


def prepare_trades(rows: Optional[List[Dict[str, Any]]] = None, table: str = '',
                   portfolio_db=None, allow_name_refresh: bool = True,
                   origin_resolver=None) -> Dict[str, Any]:
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
        optional_invalid = False
        for target, aliases in {
            'note': ('note', '备注'), 'commission': ('commission', '佣金'),
            'tax': ('tax', '印花税'),
        }.items():
            value = _pick(item['_input'], *aliases)
            if value not in (None, ''):
                if target in {'commission', 'tax'}:
                    parsed = _number(value)
                    if parsed is None or parsed < 0:
                        errors.append(f"第 {item['_row']} 行{target}必须是非负数")
                        optional_invalid = True
                    else:
                        item[target] = round(parsed, 4)
                else:
                    note = _clean_text(value)
                    if len(note) > 500:
                        errors.append(f"第 {item['_row']} 行备注不能超过 500 字符")
                        optional_invalid = True
                    else:
                        item[target] = note
        raw_run_id = _clean_text(_pick(item['_input'], 'selection_run_id'))
        raw_nomination_id = _clean_text(_pick(item['_input'], 'nomination_id'))
        raw_strategy_id = _clean_text(_pick(item['_input'], 'strategy_id'))
        if any(len(value) > 160 for value in (
                raw_run_id, raw_nomination_id, raw_strategy_id)):
            errors.append(f"第 {item['_row']} 行选股来源标识过长")
            optional_invalid = True
        if raw_strategy_id and not (raw_run_id or raw_nomination_id):
            errors.append(f"第 {item['_row']} 行 strategy_id 缺少 selection_run_id 或 nomination_id")
            optional_invalid = True
        if (raw_run_id or raw_nomination_id) and not optional_invalid:
            resolver = origin_resolver
            if resolver is None:
                try:
                    from data.research_store import ResearchStore
                    resolver = ResearchStore(ensure_schema=False).resolve_trade_origin
                except Exception:
                    resolver = None
            try:
                resolved_origin = resolver(
                    item['code'], selection_run_id=raw_run_id,
                    nomination_id=raw_nomination_id, strategy_id=raw_strategy_id,
                ) if resolver else None
            except Exception:
                resolved_origin = None
            if not resolved_origin:
                errors.append(f"第 {item['_row']} 行选股来源不存在或与股票代码不匹配")
                optional_invalid = True
            else:
                for key in ('selection_run_id', 'nomination_id', 'strategy_id'):
                    if resolved_origin.get(key):
                        item[key] = str(resolved_origin[key])
        raw_signal_id = _pick(item['_input'], 'decision_signal_id')
        if raw_signal_id not in (None, ''):
            try:
                signal_id = int(raw_signal_id)
                if signal_id <= 0:
                    raise ValueError
                from analysis.decision_signal import get_signal
                signal = get_signal(signal_id)
                signal_code = _code((signal or {}).get('code'))
                if not signal or signal_code != item['code']:
                    raise ValueError
                item['decision_signal_id'] = signal_id
            except Exception:
                errors.append(f"第 {item['_row']} 行决策信号不存在或与股票代码不匹配")
                optional_invalid = True
        if optional_invalid:
            continue
        item['source'] = _pick(item['_input'], 'source') or 'mcp:import_trades'
        for target, aliases in {
            'broker_execution_id': ('broker_execution_id', 'execution_id', '成交编号', '成交序号'),
            'broker_order_id': ('broker_order_id', 'order_id', '委托编号', '合同编号'),
            'account_ref': ('account_ref', 'account_id', '资金账号', '账户'),
        }.items():
            value = _clean_text(_pick(item['_input'], *aliases))
            if value:
                item[target] = value[:160]
        item.pop('_input', None)
        item.pop('_row', None)
        item['external_fingerprint'] = trade_execution_key(item)
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
                         allow_name_refresh: bool = True,
                         origin_resolver=None) -> Dict[str, Any]:
    """高层成交导入；默认幂等，关键字段错误/名称未解析时整批不写。"""
    if portfolio_db is None:
        from portfolio_db import portfolio_db as _portfolio_db
        portfolio_db = _portfolio_db
    prepared = prepare_trades(rows, table, portfolio_db=portfolio_db,
                              allow_name_refresh=allow_name_refresh,
                              origin_resolver=origin_resolver)
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
        keys = [trade_execution_key(row) for row in rows_to_import]
        if hasattr(portfolio_db, 'existing_trade_execution_keys'):
            try:
                legacy_candidates = [
                    _legacy_dedupe_key(row) for row in rows_to_import
                    if not (row.get('broker_execution_id') or row.get('broker_order_id')
                            or row.get('account_ref'))
                ]
                try:
                    found = portfolio_db.existing_trade_execution_keys(
                        keys, legacy_keys=legacy_candidates
                    )
                except TypeError:
                    found = portfolio_db.existing_trade_execution_keys(keys)
                existing = set(found or [])
            except Exception:
                existing = set()
        else:
            try:
                old_rows = portfolio_db.get_trades(None, 10000) or []
                existing = {trade_execution_key(row) for row in old_rows}
                existing.update(_legacy_dedupe_key(row) for row in old_rows)
            except Exception:
                existing = set()
        unique_rows = []
        for row in rows_to_import:
            key = trade_execution_key(row)
            has_external_identity = bool(
                row.get('broker_execution_id') or row.get('broker_order_id') or row.get('account_ref')
            )
            legacy_key = _legacy_dedupe_key(row)
            if key in existing or (not has_external_identity and legacy_key in existing):
                result['skipped_existing'] += 1
            else:
                unique_rows.append(row)
                existing.add(key)  # 同一批内也去重
                if not has_external_identity:
                    existing.add(legacy_key)
        rows_to_import = unique_rows
        result['prepared'] = len(rows_to_import)

    position_preview = preview_position_effects(
        rows_to_import, portfolio_db, update_position=bool(update_position)
    )
    result['position_watermark'] = position_preview['position_watermark']
    result['effects'] = position_preview['effects']
    if position_preview['errors']:
        result['errors'] = list(result['errors']) + position_preview['errors']
        result['failed'] = len(result['errors'])
        result['status'] = 'needs_input'
        result['preview'] = rows_to_import
        return result

    if dry_run:
        result['status'] = 'preview'
        result['preview'] = rows_to_import
        return result
    if not rows_to_import:
        result['status'] = 'noop'
        return result

    try:
        imported = portfolio_db.import_trades(
            rows_to_import,
            update_position=update_position,
            expected_position_watermark=position_preview['position_watermark'],
            atomic=True,
        ) or {}
    except TypeError as exc:
        # Explicit test/compatibility adapters may still implement the historical two-argument
        # protocol. Production PostgreSQL implements the strict watermark-aware contract below.
        if 'unexpected keyword argument' not in str(exc):
            raise
        imported = portfolio_db.import_trades(
            rows_to_import, update_position=update_position
        ) or {}
    result.update({key: imported.get(key, result.get(key))
                   for key in ('imported', 'failed', 'positions_updated')})
    if imported.get('errors'):
        result['errors'] = list(result['errors']) + list(imported['errors'])
    result['status'] = ('success' if not result['failed'] else
                        ('failed' if not result['imported'] else 'partial'))
    return result
