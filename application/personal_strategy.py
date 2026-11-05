"""Version-checked updates to existing personal parameters, never policy publication."""
from __future__ import annotations
import json
import math
import uuid

# Bounds for the existing user_strategy_config keys, not a second source of defaults.
BOUNDS = {
    'max_position_pct': (0.01, 100), 'max_add_times': (0, 100),
    'fundamental_min_score': (0, 100), 'price_max': (0.01, 100000),
    'hot_sector_top_n': (1, 500), 'short_term_low_pct': (0, 100),
    'historical_low_pct': (0, 100), 'drop_trigger_pct_today': (0.01, 100),
    'drop_trigger_pct_holding': (0.01, 100), 'profit_take_1_pct': (0.01, 10000),
    'profit_take_2_pct': (0.01, 10000), 'profit_take_3_pct': (0.01, 10000),
    'ma_stop_short': (2, 500), 'ma_stop_long': (2, 1000),
    'observation_drop_low': (0, 100), 'observation_drop_high': (0, 100),
}


def validate_change(defaults, current, key, value):
    if key not in defaults or (key not in BOUNDS and key != 'enable_ma_stop_loss'):
        raise ValueError('unknown_strategy_parameter')
    kind = defaults[key]['type']
    if kind == 'bool':
        if type(value) is not bool:
            raise ValueError('invalid_strategy_type')
    else:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('invalid_strategy_type')
        if kind == 'int' and type(value) is not int:
            raise ValueError('invalid_strategy_type')
        low, high = BOUNDS[key]
        if not low <= value <= high:
            raise ValueError('strategy_value_out_of_range')
    proposed = {**current, key: value}
    for names in [('profit_take_1_pct', 'profit_take_2_pct', 'profit_take_3_pct'),
                  ('ma_stop_short', 'ma_stop_long'), ('observation_drop_low', 'observation_drop_high')]:
        if key in names and any(proposed[a] >= proposed[b] for a, b in zip(names, names[1:])):
            raise ValueError('strategy_parameter_order')
    return proposed


def update_parameter(key, value, expected_value, reason, dry_run, *, actor):
    import user_strategy_config as strategy
    from db_compat import connect
    conn = connect(strategy._DB_PATH)
    try:
        cur = conn.cursor()
        # Serialize cooperating writers, including an initially absent key.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('foliant.personal-strategy'))")
        cur.execute('SELECT key,value_json FROM user_strategy_config FOR UPDATE')
        stored = {row[0]: json.loads(row[1]) if isinstance(row[1], str) else row[1] for row in cur.fetchall()}
        current = {key: stored.get(key, meta['value']) for key, meta in strategy.DEFAULTS.items()}
        validate_change(strategy.DEFAULTS, current, key, value)
        before = current[key]
        if (type(before) is bool) != (type(expected_value) is bool) or before != expected_value:
            if type(before) is type(value) and before == value:
                return {'status': 'unchanged', 'key': key, 'before': before, 'after': before}
            return {'status': 'conflict', 'key': key, 'current': before, 'expected': expected_value}
        result = {'status': 'preview' if dry_run else 'complete', 'key': key,
                  'before': before, 'after': value, 'actor': actor, 'reason': reason, 'dry_run': dry_run}
        if dry_run:
            return result
        cur.execute('''INSERT INTO user_strategy_config(key,value_json,updated_at) VALUES (?,?,NOW())
                       ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json,updated_at=NOW()
                       WHERE user_strategy_config.value_json::jsonb=?::jsonb RETURNING value_json''',
                    (key, json.dumps(value), json.dumps(before)))
        updated = cur.fetchone()
        if updated is None:
            cur.execute('SELECT value_json FROM user_strategy_config WHERE key=?', (key,))
            actual = cur.fetchone()[0]
            return {'status': 'conflict', 'key': key, 'current': json.loads(actual) if isinstance(actual, str) else actual}
        readback = updated[0]
        result['after'] = json.loads(readback) if isinstance(readback, str) else readback
        from application.results import canonical_json, payload_hash, now_iso
        receipt_id = 'sp_' + uuid.uuid4().hex
        result['receipt'] = 'shadow://foliant/strategy-parameters/' + receipt_id
        result['created_at'] = now_iso()
        request = {'key': key, 'value': value, 'expected_value': expected_value, 'reason': reason}
        cur.execute('''INSERT INTO foliant_write_idempotency
                       (actor_id,action,idempotency_key,request_hash,response_payload,created_at)
                       VALUES (?,?,?,?,?,?)''',
                    (actor, 'strategy.parameter.update', receipt_id, payload_hash(request),
                     canonical_json(result), result['created_at']))
        conn.commit()
        return result
    finally:
        conn.rollback()
        conn.close()


def parameter_history(*, actor, limit=30):
    from db_compat import connect
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT response_payload FROM foliant_write_idempotency WHERE actor_id=? AND action=? ORDER BY created_at DESC LIMIT ?",
                    (actor, 'strategy.parameter.update', limit))
        return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in cur.fetchall()]
    finally:
        conn.close()
