import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""AI 推荐股票后台价格监控 — 借鉴 go-stock MonitorAiRecommendStockPrices

闭环用户体验：
  AI 任意分析输出"推荐买入 / 目标价 / 止损价" → save_recommendation 入库
   → enable_monitor 推入 monitored_stocks（如已存在则更新）
   → 盘后任务 jobs_hub.task_eod_outcomes(16:55)用收盘价回填胜率(check_all_active)
   → 触发 take_profit / stop_loss 时通过 notification_router 推送
   → 回填 hit_target_at / hit_stop_at

接口：
  save_recommendation(...)      记录新推荐
  list_active(symbol=None)      列活跃推荐
  enable_monitor(rec_id)        推入监控池
  mark_hit(rec_id, hit_type)    回填触发
  check_all_active(notify_fn)   后台任务用 — 拉实时价并触发
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Any

from db_compat import connect as db_connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')

# 推荐过期天数:超过这么久仍未触发止盈/止损 → 视为已了结(按当时浮盈浮亏记真实收益,计入评估)
PENDING_EXPIRE_DAYS = 90

# 真实盈亏追踪所需的列(老库可能没有,首次用时幂等补齐)
_PERF_COLS = [
    ('ref_price', 'DOUBLE PRECISION'),       # 入场参考价(推荐当时市价,算真实收益的基准)
    ('last_price', 'DOUBLE PRECISION'),      # 最近一次观测价(check_all_active 每轮更新)
    ('last_price_at', 'TIMESTAMPTZ'),
    ('realized_pnl_pct', 'DOUBLE PRECISION'),  # 了结时的真实收益%(止盈/止损/过期)
    ('closed_at', 'TIMESTAMPTZ'),
    ('close_reason', 'TEXT'),                # target / stop / expired
]
_perf_cols_ready = False


def _ensure_perf_columns():
    """幂等补齐真实盈亏追踪列(PG)。每进程跑一次;SQLite 模式该表通常不存在,best-effort。"""
    global _perf_cols_ready
    if _perf_cols_ready:
        return
    try:
        conn = db_connect(_DB_PATH)
        cur = conn.cursor()
        for col, typ in _PERF_COLS:
            try:
                if USE_POSTGRES:
                    cur.execute(f'ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS {col} {typ}')
                else:
                    cur.execute(f'ALTER TABLE ai_recommendations ADD COLUMN {col} {typ}')
            except Exception:
                pass  # 列已存在 / 表不存在
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[ai_rec_monitor] 补列失败(忽略): {e}')
    _perf_cols_ready = True


def _current_price(symbol: str) -> Optional[float]:
    """取实时价,失败返回 None。"""
    try:
        import datahub
        q = datahub.quote(symbol)
        p = q.get('price') if isinstance(q, dict) else None
        return float(p) if p not in (None, '') else None
    except Exception:
        return None


def save_recommendation(symbol: str, name: str = '', source: str = '',
                        rating: str = '', confidence: str = '',
                        target_price: Optional[float] = None,
                        entry_low: Optional[float] = None,
                        entry_high: Optional[float] = None,
                        take_profit: Optional[float] = None,
                        stop_loss: Optional[float] = None,
                        reason: str = '', ref_price: Optional[float] = None) -> int:
    """记录一条新 AI 推荐，返回插入的 id。

    去重:同一 symbol+source 当天若已有活跃推荐,跳过插入并返回已有 id。
    (否则每日策略扫描重荐同一只 → active 表堆重复行 → ai_rec_check 重复监控/告警)
    ref_price: 入场参考价(算真实收益的基准)。不传则取实时价,再不行用 entry 区间中值。
    """
    _ensure_perf_columns()
    # 分类值归一(LLM 多出英文 buy/strong_buy/hold;DB 端中文 CHECK 约束,这里统一成中文规范值)
    try:
        from enums import normalize_rating, normalize_confidence
        rating = normalize_rating(rating) if rating else rating
        confidence = normalize_confidence(confidence) if confidence else confidence
    except Exception:
        pass
    # 入场参考价:显式传入 > 实时价 > entry 区间中值
    if ref_price is None:
        ref_price = _current_price(symbol)
    if ref_price is None and (entry_low or entry_high):
        lo, hi = entry_low or entry_high, entry_high or entry_low
        ref_price = (float(lo) + float(hi)) / 2
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    # —— 当日同 symbol+source 去重 ——
    try:
        if USE_POSTGRES:
            cur.execute('''SELECT id FROM ai_recommendations
                           WHERE symbol=? AND source=? AND is_active=TRUE
                             AND recommended_at::date = CURRENT_DATE
                           ORDER BY id DESC LIMIT 1''', (symbol, source))
        else:
            cur.execute('''SELECT id FROM ai_recommendations
                           WHERE symbol=? AND source=? AND is_active=1
                             AND DATE(recommended_at)=DATE('now')
                           ORDER BY id DESC LIMIT 1''', (symbol, source))
        dup = cur.fetchone()
        if dup:
            conn.close()
            return int(dup[0])
    except Exception as e:
        print(f'[ai_rec_monitor] 去重检查失败,继续插入: {e}')
    cur.execute('''
        INSERT INTO ai_recommendations
          (symbol, name, source, rating, confidence,
           target_price, entry_low, entry_high, take_profit, stop_loss, reason, ref_price)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (symbol, name, source, rating, confidence,
          target_price, entry_low, entry_high, take_profit, stop_loss, reason, ref_price))
    new_id = cur.lastrowid  # db_compat 内部 PG 模式用 lastval() 模拟
    conn.commit()
    conn.close()
    return int(new_id) if new_id else 0


def list_active(symbol: Optional[str] = None, only_monitored: bool = False,
                limit: int = 100) -> List[Dict[str, Any]]:
    sql = '''
        SELECT id, symbol, name, source, rating, confidence,
               target_price, entry_low, entry_high, take_profit, stop_loss,
               reason, is_monitored, hit_target_at, hit_stop_at, recommended_at, ref_price
        FROM ai_recommendations
        WHERE is_active = {true} AND hit_target_at IS NULL AND hit_stop_at IS NULL
    '''.format(true='TRUE' if USE_POSTGRES else '1')
    params = []
    if symbol:
        sql += ' AND symbol = ?'; params.append(symbol)
    if only_monitored:
        sql += ' AND is_monitored = {true}'.format(true='TRUE' if USE_POSTGRES else '1')
    sql += ' ORDER BY recommended_at DESC LIMIT ?'
    params.append(limit)
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute(sql, tuple(params))
    rows = cur.fetchall()
    conn.close()
    keys = ['id', 'symbol', 'name', 'source', 'rating', 'confidence',
            'target_price', 'entry_low', 'entry_high', 'take_profit', 'stop_loss',
            'reason', 'is_monitored', 'hit_target_at', 'hit_stop_at', 'recommended_at', 'ref_price']
    return [dict(zip(keys, r)) for r in rows]


def enable_monitor(rec_id: int) -> bool:
    """把推荐标 is_monitored=True，并 upsert 到 monitored_stocks 触发后台监控"""
    rec = _get(rec_id)
    if not rec:
        return False
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute('UPDATE ai_recommendations SET is_monitored = TRUE, updated_at = NOW() WHERE id = ?', (rec_id,))
    else:
        cur.execute('UPDATE ai_recommendations SET is_monitored = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (rec_id,))
    conn.commit()
    conn.close()

    try:
        _upsert_monitored_stock(rec)
    except Exception as e:
        print(f'[ai_rec_monitor] upsert monitored_stocks 失败: {e}')

    return True


def _get(rec_id: int) -> Optional[Dict[str, Any]]:
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        SELECT id, symbol, name, source, rating, confidence,
               target_price, entry_low, entry_high, take_profit, stop_loss, reason
        FROM ai_recommendations WHERE id = ?
    ''', (rec_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    keys = ['id', 'symbol', 'name', 'source', 'rating', 'confidence',
            'target_price', 'entry_low', 'entry_high', 'take_profit', 'stop_loss', 'reason']
    return dict(zip(keys, row))


def _upsert_monitored_stock(rec: Dict[str, Any]):
    """把推荐写到 monitored_stocks（被 smart_monitor / monitor_scheduler 实时跟踪）"""
    import json
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    entry_range = json.dumps({'low': rec.get('entry_low'), 'high': rec.get('entry_high')},
                             ensure_ascii=False)
    if USE_POSTGRES:
        cur.execute('''
            INSERT INTO monitored_stocks
              (symbol, name, rating, entry_range, take_profit, stop_loss, notification_enabled)
            VALUES (?,?,?,?::jsonb,?,?, TRUE)
            ON CONFLICT (symbol) DO UPDATE SET
              name = EXCLUDED.name,
              rating = EXCLUDED.rating,
              entry_range = EXCLUDED.entry_range,
              take_profit = EXCLUDED.take_profit,
              stop_loss = EXCLUDED.stop_loss,
              notification_enabled = TRUE,
              updated_at = NOW()
        ''', (rec['symbol'], rec['name'], rec['rating'], entry_range,
              rec.get('take_profit'), rec.get('stop_loss')))
    else:
        cur.execute('''
            INSERT INTO monitored_stocks
              (symbol, name, rating, entry_range, take_profit, stop_loss, notification_enabled)
            VALUES (?,?,?,?,?,?, 1)
            ON CONFLICT (symbol) DO UPDATE SET
              name = excluded.name,
              rating = excluded.rating,
              entry_range = excluded.entry_range,
              take_profit = excluded.take_profit,
              stop_loss = excluded.stop_loss,
              notification_enabled = 1,
              updated_at = CURRENT_TIMESTAMP
        ''', (rec['symbol'], rec['name'], rec['rating'], entry_range,
              rec.get('take_profit'), rec.get('stop_loss')))
    conn.commit()
    conn.close()


def mark_hit(rec_id: int, hit_type: str):
    """hit_type: 'target' | 'stop'"""
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()
    field = 'hit_target_at' if hit_type == 'target' else 'hit_stop_at'
    if USE_POSTGRES:
        cur.execute(f'UPDATE ai_recommendations SET {field} = NOW(), updated_at = NOW() WHERE id = ?', (rec_id,))
    else:
        cur.execute(f'UPDATE ai_recommendations SET {field} = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (rec_id,))
    conn.commit()
    conn.close()


def _ref_of(rec: Dict[str, Any]) -> Optional[float]:
    """取入场参考价:ref_price 优先,缺失则用 entry 区间中值兜底(老数据)。"""
    ref = rec.get('ref_price')
    if ref:
        return float(ref)
    lo, hi = rec.get('entry_low'), rec.get('entry_high')
    if lo or hi:
        a, b = lo or hi, hi or lo
        return (float(a) + float(b)) / 2
    return None


def _pnl_pct(ref: Optional[float], price: Optional[float]) -> Optional[float]:
    if not ref or not price:
        return None
    return round((float(price) - float(ref)) / float(ref) * 100, 2)


def check_all_active(notify_fn=None) -> Dict[str, int]:
    """后台任务用：拉所有 active 推荐的实时价，对比触发条件，回填真实盈亏 + 推送。

    每轮:更新 last_price;命中止盈/止损或超期(PENDING_EXPIRE_DAYS) → 了结并写 realized_pnl_pct。
    """
    import time as _time
    _t_total = _time.time()
    _ensure_perf_columns()
    active = list_active(only_monitored=True, limit=500)
    if not active:
        return {'checked': 0, 'hit_target': 0, 'hit_stop': 0, 'expired': 0}
    print(f'[ai_rec_check] 活跃推荐 {len(active)} 条, 开始批量拉价', flush=True)

    stats = {'checked': 0, 'hit_target': 0, 'hit_stop': 0, 'expired': 0}
    NOW = 'NOW()' if USE_POSTGRES else 'CURRENT_TIMESTAMP'
    conn = db_connect(_DB_PATH)
    cur = conn.cursor()

    _INA = 'FALSE' if USE_POSTGRES else '0'

    def _close(rec_id, kind, hit_field, realized):
        """了结一条推荐:写 hit_*_at(若有) + closed_at/close_reason/realized_pnl_pct + is_active=0。
        ⚠️ 必须置 is_active=0:否则到期了结(无 hit_*_at)的推荐仍被 list_active 反复捞回重复处理,
           active 表里堆积"僵尸已了结"行,每轮 check 都白跑一遍。"""
        sets = [f'closed_at={NOW}', 'close_reason=?', 'realized_pnl_pct=?',
                f'is_active={_INA}', f'updated_at={NOW}']
        params: List[Any] = [kind, realized]
        if hit_field:
            sets.insert(0, f'{hit_field}={NOW}')
        cur.execute(f'UPDATE ai_recommendations SET {", ".join(sets)} WHERE id=?', tuple(params) + (rec_id,))

    # 一次批量取价(对齐下方 candidate 分支):原来逐只 _current_price → 监控池满时每轮最多 500 次
    # 串行单股 HTTP 往返、每 30min 一轮、全交易时段约 11 轮/日。批量 datahub.quotes 压成 1 次请求。
    _qmap = {}
    try:
        import datahub
        _qmap = datahub.quotes([r['symbol'] for r in active]) or {}
    except Exception:
        _qmap = {}

    def _price_of(sym):
        q = _qmap.get(str(sym).zfill(6)) or _qmap.get(str(sym)) or {}
        p = q.get('price') if isinstance(q, dict) else None
        try:
            return float(p) if p not in (None, '', 0, '0') else _current_price(sym)
        except (TypeError, ValueError):
            return _current_price(sym)

    for rec in active:
        symbol = rec['symbol']
        try:
            price = _price_of(symbol)
            if price is None:
                continue
            stats['checked'] += 1
            ref = _ref_of(rec)
            # 每轮更新最近观测价(供 pending 浮盈浮亏评估)
            cur.execute(f'UPDATE ai_recommendations SET last_price=?, last_price_at={NOW} WHERE id=?',
                        (price, rec['id']))
            tp, sl = rec.get('take_profit'), rec.get('stop_loss')
            # realized 用**委托价(tp/sl)**口径而非 30min 快照价:盘中两次快照间跳空穿越 tp/sl 时,
            # 快照价可能远低于 sl(或高于 tp)→ 把"纸面止损"记成更大亏损,系统性下偏该 source 的 avg_return。
            if tp and price >= tp:
                # AI 推荐池(ai_recommendations)只做后台 paper-trading 跟踪 / 胜率统计,不推送通知。
                _close(rec['id'], 'target', 'hit_target_at', _pnl_pct(ref, float(tp)))
                stats['hit_target'] += 1
            elif sl and price <= sl:
                _close(rec['id'], 'stop', 'hit_stop_at', _pnl_pct(ref, float(sl)))
                stats['hit_stop'] += 1
            else:
                # 超期未触发 → 按当前浮盈浮亏了结,计入评估(消除"永远 pending"的幸存者偏差)
                try:
                    age = (datetime.now() - _parse_dt(rec['recommended_at'])).days
                except Exception:
                    age = 0
                if age >= PENDING_EXPIRE_DAYS:
                    _close(rec['id'], 'expired', None, _pnl_pct(ref, price))
                    stats['expired'] += 1
        except Exception as e:
            print(f'[ai_rec_monitor] {symbol} 检查失败: {e}')
            continue
    conn.commit()
    conn.close()

    # ── 非监控 candidate(如 unified_selection 入池但不主动监控)也刷 last_price + 到期了结 ──
    # 否则评估端无 last_price → 这些 source 的真实胜率永远算不出(wf_selection_to_rec 等闭环空转)。
    # 语义保持"零成本记录":只批量刷价 + 超期(PENDING_EXPIRE_DAYS)按浮盈了结,不设止盈止损、不推送。
    try:
        cands = [r for r in list_active(only_monitored=False, limit=1000) if not r.get('is_monitored')]
        if cands:
            import datahub
            qmap = datahub.quotes([c['symbol'] for c in cands]) or {}
            conn3 = db_connect(_DB_PATH)
            cur3 = conn3.cursor()
            for rec in cands:
                try:
                    q = qmap.get(str(rec['symbol']).zfill(6)) or qmap.get(rec['symbol']) or {}
                    price = q.get('price')
                    if not price:
                        continue
                    price = float(price)
                    stats['checked'] += 1
                    cur3.execute(f'UPDATE ai_recommendations SET last_price=?, last_price_at={NOW} WHERE id=?',
                                 (price, rec['id']))
                    try:
                        age = (datetime.now() - _parse_dt(rec['recommended_at'])).days
                    except Exception:
                        age = 0
                    if age >= PENDING_EXPIRE_DAYS:
                        cur3.execute(
                            f'UPDATE ai_recommendations SET closed_at={NOW}, close_reason=?, '
                            f'realized_pnl_pct=?, is_active={"FALSE" if USE_POSTGRES else "0"}, '
                            f'updated_at={NOW} WHERE id=?',
                            ('expired', _pnl_pct(_ref_of(rec), price), rec['id']))
                        stats['expired'] += 1
                except Exception:
                    continue
            conn3.commit()
            conn3.close()
    except Exception as e:
        print(f'[ai_rec_monitor] candidate 刷价失败: {e}')

    # 额外检查 monitored_stocks 表（老系统：带止盈止损价的持仓监控）
    try:
        if USE_POSTGRES:
            conn2 = db_connect(_DB_PATH)
            cur2 = conn2.cursor()
            cur2.execute('''
                SELECT id, symbol, name, current_price, take_profit, stop_loss
                FROM monitored_stocks
                WHERE (take_profit IS NOT NULL OR stop_loss IS NOT NULL)
                  AND current_price IS NOT NULL
            ''')
            for row in cur2.fetchall():
                mid, msym, mname, mprice, mtp, msl = row
                if mtp and mprice and mprice >= float(mtp):
                    msg = f"🎯 持仓止盈 {msym} {mname}：目标价 {mtp}，现价 {mprice}"
                    _notify(notify_fn, msym, mname, msg)
                    stats['hit_target'] += 1
                elif msl and mprice and mprice <= float(msl):
                    msg = f"⛔ 持仓止损 {msym} {mname}：止损价 {msl}，现价 {mprice}"
                    _notify(notify_fn, msym, mname, msg)
                    stats['hit_stop'] += 1
            conn2.close()
    except Exception:
        pass

    print(f'[ai_rec_check] 完成 耗时 {_time.time()-_t_total:.1f}s | '
          f'checked={stats["checked"]} target={stats["hit_target"]} '
          f'stop={stats["hit_stop"]} expired={stats["expired"]}', flush=True)
    return stats


def _parse_dt(val):
    """容错解析时间(datetime / ISO字符串)→ **naive 本地时间**。
    ⚠️ 2026-07-17 修:PG 的 recommended_at 是 timestamptz,psycopg2 返回 tz-aware datetime,
    与 naive 的 datetime.now() 相减抛 `TypeError: can't subtract offset-naive and offset-aware`,
    被 check_all_active 的 `except: age=0` 吞掉 → 90 天到期了结永不触发(candidate 推荐无限堆积、
    'expired' 桶恒空)。SQLite 返字符串走 strptime 路径本就是 naive,故本地测不出。统一归一 naive。"""
    if isinstance(val, datetime):
        return val.astimezone().replace(tzinfo=None) if val.tzinfo is not None else val
    s = str(val).replace('T', ' ').replace('+00:00', '')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(s[:19])


def _notify(notify_fn, symbol: str, name: str, msg: str):
    if notify_fn:
        try:
            notify_fn(symbol, name, msg)
            return
        except Exception:
            pass
    try:
        from notification_router import send
        send('alert', f'AI 推荐告警 - {name}({symbol})', msg)
    except Exception:
        print(f'[ai_rec_monitor] [{symbol} {name}] {msg}')


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== AI 推荐监控自检 ===')
    rid = save_recommendation(
        symbol='600519', name='茅台', source='test',
        rating='buy', confidence='中',
        target_price=1500, entry_low=1280, entry_high=1320,
        take_profit=1500, stop_loss=1200,
        reason='估值锚定 + 现金流稳定 (test)',
    )
    print(f'新增推荐 id={rid}')
    print('\n活跃推荐:')
    for r in list_active(limit=5):
        print(f"  #{r['id']} {r['symbol']} {r['name']} 评级={r['rating']} "
              f"目标={r['target_price']} 止盈={r['take_profit']} 止损={r['stop_loss']} "
              f"监控={r['is_monitored']}")
    if rid:
        ok = enable_monitor(rid)
        print(f'\nenable_monitor({rid}) = {ok}')
