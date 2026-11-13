from __future__ import annotations
import ast
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
import _bootstrap
from application import personal_skill as skill
from application.personal_strategy import validate_change
from application.run_repository import RunRepository
from application.services import ApplicationError, TradeEntryService, PortfolioAccessService
from test_trade_entry_application import FakePortfolioDB, _row

CONFIG = {'actor': 'synthetic-owner', 'allow_writes': True}
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'agent/skills/foliant-personal-portfolio/scripts/foliant'


@pytest.fixture()
def domain(tmp_path, monkeypatch):
    db = FakePortfolioDB()
    repo = RunRepository(connect_fn=lambda _: sqlite3.connect(tmp_path/'receipts.db'), is_postgres=False)
    app = SimpleNamespace(trade_entry=TradeEntryService(repo, portfolio_db=db), portfolio=PortfolioAccessService(portfolio_db=db))
    monkeypatch.setattr(skill, 'services', lambda: app)
    return db, app


def test_import_readback_and_retry_use_one_domain_batch(domain):
    db, app = domain
    args = {'rows': [_row()], 'idempotency_key': 'synthetic-message-001'}
    first = skill.invoke('trades.import', args, CONFIG)
    repeated = skill.invoke('trades.import', args, CONFIG)
    assert first['status'] == 'complete' and first['receipt']
    assert repeated['receipt'] == first['receipt'] and repeated['replayed']
    assert db.import_calls == 1
    assert first['records'][0]['commission'] == 5
    assert first['records'][0]['source'] == 'skill:foliant-personal-portfolio'
    reader = {'actor': CONFIG['actor']}
    by_batch = skill.invoke('trades.receipt', {'batch_id': first['batch_id']}, reader)
    by_key = skill.invoke('trades.receipt', {'idempotency_key': args['idempotency_key']}, reader)
    assert by_key == by_batch == {k: v for k, v in first.items() if k != 'replayed'}
    records = skill.invoke('trades.list', {'limit': 1}, CONFIG)['data']['records']
    assert records[0]['code'] == '600519'
    for selector in [{'batch_id': first['batch_id']}, {'idempotency_key': args['idempotency_key']}]:
        with pytest.raises(ApplicationError) as missing:
            skill.invoke('trades.receipt', selector, {'actor': 'different-owner'})
        assert (missing.value.code, missing.value.status_code) == ('trade_review_not_found', 404)
    with pytest.raises(ApplicationError) as conflict:
        skill.invoke('trades.import', {**args, 'rows': [_row(price=1600)]}, CONFIG)
    assert conflict.value.code == 'idempotency_conflict'
    assert skill.invoke('trades.receipt', {'idempotency_key': args['idempotency_key']}, reader) == by_key
    assert db.import_calls == 1


def test_receipt_before_commit_distinguishes_missing_processing_and_pending(domain):
    db, app = domain
    entry = app.trade_entry
    key = 'synthetic-message-002'
    selector = {'idempotency_key': key}
    reader = {'actor': CONFIG['actor']}
    with pytest.raises(ApplicationError) as missing:
        skill.invoke('trades.receipt', selector, reader)
    assert missing.value.code == 'trade_review_not_found'
    fields = {'trades': [_row()], 'table': '', 'updatePosition': True}
    source = 'skill:foliant-personal-portfolio'
    payload = {'fields': fields, 'source': source}
    assert entry.repository.claim_write(reader['actor'], entry.review_create_action, key, payload) == (True, None)
    with pytest.raises(ApplicationError) as processing:
        skill.invoke('trades.receipt', selector, reader)
    assert processing.value.code == 'write_in_progress'
    assert db.import_calls == 0 and not db.batches
    entry.repository.release_write_claim(reader['actor'], entry.review_create_action, key, payload)
    staged = entry.create_review(actor_id=reader['actor'], fields=fields, idempotency_key=key, source=source)
    pending = skill.invoke('trades.receipt', selector, reader)
    assert (pending['batch_id'], pending['status'], pending['receipt']) == (staged['review_id'], 'pending', None)
    assert db.import_calls == 0
    imported = skill.invoke('trades.import', {**selector, 'rows': fields['trades']}, CONFIG)
    assert imported['status'] == 'complete' and db.import_calls == 1
    other = {**CONFIG, 'actor': 'different-owner'}
    second = skill.invoke('trades.import', {**selector, 'rows': [_row(price=1600)]}, other)
    assert second['batch_id'] != imported['batch_id']
    for actor, expected in [(reader, imported), (other, second)]:
        assert skill.invoke('trades.receipt', selector, actor)['receipt'] == expected['receipt']


def test_receipt_requires_exactly_one_selector(domain):
    reader = {'actor': CONFIG['actor']}
    cap = next(item for item in skill.catalog(reader) if item['name'] == 'trades.receipt')
    schema = cap['inputSchema']
    assert cap['annotations']['readOnlyHint']
    assert schema['minProperties'] == schema['maxProperties'] == 1
    assert set(schema['properties']) == {'batch_id', 'idempotency_key'}
    for selector in [{}, {'batch_id': 'batch-01', 'idempotency_key': 'synthetic-key'}]:
        with pytest.raises(ValueError, match='provide_batch_id_or_idempotency_key'):
            skill.invoke('trades.receipt', selector, reader)
    for selector in [{'idempotency_key': 'short'}, {'batch_id': None}, {'idempotency_key': None},
                     {'idempotency_key': 'synthetic-key', 'actor': 'different-owner'}]:
        with pytest.raises(ValidationError):
            skill.invoke('trades.receipt', selector, reader)


def test_receipt_and_import_normalize_the_same_key(domain):
    db, _ = domain
    for key in [' ' * 8, '   short   ']:
        for name, args in [('trades.import', {'rows': [_row()]}), ('trades.receipt', {})]:
            with pytest.raises(ApplicationError) as invalid:
                skill.invoke(name, {**args, 'idempotency_key': key}, CONFIG)
            assert invalid.value.code == 'idempotency_key_required'
    assert not db.batches and db.import_calls == 0
    args = {'rows': [_row()], 'idempotency_key': '  synthetic-key  '}
    saved = skill.invoke('trades.import', args, CONFIG)
    for key in [args['idempotency_key'], args['idempotency_key'].strip()]:
        assert skill.invoke('trades.receipt', {'idempotency_key': key}, CONFIG)['receipt'] == saved['receipt']
    assert db.import_calls == 1


def test_missing_facts_oversell_and_preview_never_write(domain):
    db, app = domain
    row = _row(); row['trade_type'] = '卖出'
    result = skill.invoke('trades.import', {'rows': [row], 'idempotency_key': 'synthetic-sell-001'}, CONFIG)
    assert result['status'] == 'needs_input' and result['errors']
    assert db.import_calls == 0
    result = skill.invoke('trades.preview', {'rows': [_row()]}, CONFIG)
    assert result['status'] == 'ready' and not db.batches
    assert db.import_calls == 0
    with pytest.raises(ValueError):
        skill.invoke('trades.import', {'rows': [_row()], 'table': 'extra', 'idempotency_key': 'synthetic-002'}, CONFIG)


def test_ledger_only_and_markdown_import(domain):
    db, app = domain
    table = '|股票代码|股票名称|交易类型|成交量|成交价|成交时间|\n|---|---|---|---|---|---|\n|600519|示例|卖出|100|10|2026-01-05 10:30:00|'
    result = skill.invoke('trades.import', {'table': table, 'update_position': False, 'idempotency_key': 'synthetic-table-001'}, CONFIG)
    assert result['status'] == 'complete' and result['update_position'] is False
    assert db.import_calls == 1


def test_discovery_permissions_and_strict_arguments():
    readonly = skill.catalog({'actor': 'reader'})
    assert all(c['effect'] == 'read' for c in readonly)
    assert 'trades.list' in {c['name'] for c in readonly}
    for name, args in [('trades.import', {}), ('orders.buy', {})]:
        with pytest.raises(ValueError): skill.invoke(name, args, {'actor': 'reader'})
    for name, args in [('trades.list', {'limit': 0}), ('stock.info', {'code': '600519; echo secret'}),
                       ('trades.list', {'limit': '1'}), ('trades.list', {'actor': 'another'})]:
        with pytest.raises(ValidationError): skill.invoke(name, args, CONFIG)


def test_real_cli_catalog_and_invalid_input_are_json(tmp_path):
    config = tmp_path/'config.json'; config.write_text(json.dumps(CONFIG))
    command = [sys.executable, str(CLI), '--config', str(config)]
    catalog = subprocess.run(command+['catalog'], capture_output=True, text=True)
    assert catalog.returncode == 0
    names = {item['name'] for item in json.loads(catalog.stdout)}
    assert {'trades.list', 'trades.import', 'strategy.update', 'strategy.history'} <= names
    assert 'orders.buy' not in names
    failed = subprocess.run(command+['call', 'trades.list'], input='{"limit":"1"}', capture_output=True, text=True)
    assert failed.returncode == 1
    assert json.loads(failed.stdout)['code'] == 'ValidationError'
    assert 'password' not in failed.stdout.lower()


def test_all_lazy_targets_exist_and_accept_catalog_parameters():
    # Validate contracts without importing optional data providers or connecting to a database.
    search = [ROOT] + [Path(p) for p in sys.path if str(ROOT) in p]
    for cap in skill.CAPABILITIES.values():
        if cap.target.startswith('@'):
            fn = getattr(skill, cap.target[1:]); import inspect
            inspect.signature(fn).bind_partial(config=CONFIG, **{k: None for k in cap.model.model_fields})
        elif ':' in cap.target:
            module, name = cap.target.split(':')
            path = next((root / (module.replace('.', '/')+'.py') for root in search if (root / (module.replace('.', '/')+'.py')).exists()), None)
            assert path, cap.target
            tree = ast.parse(path.read_text())
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            accepted = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            assert set(cap.model.model_fields) <= accepted, cap.target
            required = {a.arg for a in fn.args.args[:len(fn.args.args)-len(fn.args.defaults)]}
            required.update(a.arg for a, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if default is None)
            assert required <= set(cap.model.model_fields), cap.target


def test_strategy_types_ranges_and_related_thresholds():
    from user_strategy_config import DEFAULTS
    current = {k: v['value'] for k, v in DEFAULTS.items()}
    assert validate_change(DEFAULTS, current, 'max_position_pct', 8)['max_position_pct'] == 8
    for key, value in [('max_position_pct', 101), ('max_position_pct', float('nan')), ('max_add_times', True),
                       ('max_add_times', 1.5), ('enable_ma_stop_loss', 1), ('profit_take_2_pct', 10),
                       ('ma_stop_short', 90), ('observation_drop_low', 50), ('fake', 1)]:
        with pytest.raises(ValueError): validate_change(DEFAULTS, current, key, value)
