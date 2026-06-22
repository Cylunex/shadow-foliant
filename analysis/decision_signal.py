import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""决策信号统一层(DecisionSignal)—— 借鉴 daily_stock_analysis #1390(只借思路,按本项目重写)。

把散落各处的"AI 给了什么操作建议"(深度分析 final_decision / 选股 / 盯盘 / 手动)统一抽成
**一条带生命周期 + 去重 + 后验校验的结构化信号**,从此可查询、可统计胜率、可联动持仓。
与已有"AI 推荐池"(ai_recommendations,只装选股票)互补:本层覆盖**每一次分析决策**,
口径更全,且带 8 态动作分类 + 反向信号自动作废 + 按维度冻结胜率。

8 态动作(action):buy 买入 / add 增持 / hold 持有 / reduce 减持 / sell 卖出 /
                   watch 观望 / avoid 回避 / alert 预警。方向(出场后验用):
  buy/add → 期望涨(+1);sell/reduce → 期望跌(-1);hold/watch/avoid/alert → 中性(0,不计胜负)。

生命周期 status:active 活跃 → expired 过期(超 expires_at,惰性置) /
                invalidated 被反向信号作废 / closed 手动关闭 / archived 归档。终态不可逆回 active。

存储走 db_compat(SQLite/PG 双模),与 ai_recommendations / llm_usage 同库 jobs_snapshots.db。

接口:
  create_signal(...) -> (id, created)         # 幂等去重 + 反向作废 + 惰性过期
  extract_from_analysis(result) -> id|None    # 从 analyze_single_stock_for_batch 结果抽取并落库
  list_signals(...) / get_latest_active(code) / update_status(id, status)
  run_outcomes(days, ...) -> dict             # 后验:按 horizon 用 K线判 hit/miss/neutral
  outcome_stats(dimension, days) -> dict       # 按 action/source/horizon 分桶胜率
"""

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from db_compat import connect as _connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')
_tables_ready = False
ENGINE_VERSION = 'decision-signal-v1'

# 动作分类与方向
ACTIONS = ('buy', 'add', 'hold', 'reduce', 'sell', 'watch', 'avoid', 'alert')
ACTION_CN = {'buy': '买入', 'add': '增持', 'hold': '持有', 'reduce': '减持',
             'sell': '卖出', 'watch': '观望', 'avoid': '回避', 'alert': '预警'}
_ACTION_DIR = {'buy': 1, 'add': 1, 'sell': -1, 'reduce': -1,
               'hold': 0, 'watch': 0, 'avoid': 0, 'alert': 0}
TERMINAL_STATUS = ('expired', 'invalidated', 'closed', 'archived')

# 持有周期文本 → 后验天数(交易日近似)
_HORIZON_DAYS = {'intraday': 1, 'short': 3, 'swing': 10, 'long': 20}


def _ensure_tables():
    global _tables_ready
    if _tables_ready:
        return
    try:
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        pk = 'SERIAL PRIMARY KEY' if USE_POSTGRES else 'INTEGER PRIMARY KEY AUTOINCREMENT'
        cur.execute(f'''CREATE TABLE IF NOT EXISTS decision_signals (
            id {pk},
            code TEXT, name TEXT,
            source_type TEXT, source_ref TEXT,
            action TEXT, rating TEXT, confidence TEXT, score INTEGER,
            horizon TEXT, horizon_days INTEGER,
            ref_price REAL, entry_low REAL, entry_high REAL,
            stop_loss REAL, target_price REAL,
            reason TEXT, risk TEXT,
            status TEXT DEFAULT 'active',
            trace_id TEXT,
            created_at TEXT, expires_at TEXT, updated_at TEXT)''')
        cur.execute(f'''CREATE TABLE IF NOT EXISTS decision_signal_outcomes (
            id {pk},
            signal_id INTEGER, horizon_days INTEGER,
            anchor_date TEXT, eval_date TEXT,
            start_price REAL, end_price REAL, ret_pct REAL,
            direction_expected INTEGER, outcome TEXT,
            engine_version TEXT, created_at TEXT,
            UNIQUE(signal_id, horizon_days))''')
        for idx in (
            'CREATE INDEX IF NOT EXISTS idx_ds_code_status ON decision_signals(code, status)',
            'CREATE INDEX IF NOT EXISTS idx_ds_created ON decision_signals(created_at)',
        ):
            try:
                cur.execute(idx)
            except Exception:
                pass
        conn.commit()
        conn.close()
        _tables_ready = True
    except Exception as e:
        print(f'[decision_signal] 建表失败(忽略): {type(e).__name__}: {str(e)[:80]}')


# ── 解析助手 ────────────────────────────────────────────────────────
def _num(v) -> Optional[float]:
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r'-?\d+\.?\d*', str(v).replace(',', ''))
    return float(m.group()) if m else None


def _range(v) -> Tuple[Optional[float], Optional[float]]:
    """'12.5-13.0' → (12.5,13.0);单值 → (x,x);空 → (None,None)。"""
    if v is None or v == '':
        return (None, None)
    nums = re.findall(r'\d+\.?\d*', str(v).replace(',', ''))
    if not nums:
        return (None, None)
    if len(nums) >= 2:
        a, b = float(nums[0]), float(nums[1])
        return (min(a, b), max(a, b))
    return (float(nums[0]), float(nums[0]))


def normalize_action(rating: str = '', advice: str = '') -> str:
    """由评级 + 操作建议文本归一到 8 态动作。优先看 advice 里的细分动作词。"""
    txt = str(advice or '')
    # 否定/回避优先
    if re.search(r'不建议买|不宜买|回避|规避|不要买', txt):
        return 'avoid'
    if re.search(r'增持|加仓|逢低买|继续买', txt):
        return 'add'
    if re.search(r'减持|减仓|止盈了结|逢高减', txt):
        return 'reduce'
    if re.search(r'观望|等待|暂不', txt):
        return 'watch'
    try:
        from enums import normalize_rating
        r = normalize_rating(rating)
    except Exception:
        r = str(rating or '持有')
    if r in ('强烈买入', '买入'):
        return 'buy'
    if r in ('卖出', '强烈卖出'):
        return 'sell'
    return 'hold'


def _conf_label(confidence_level) -> Tuple[str, Optional[int]]:
    """final_decision 的 confidence_level 可能是 1-10 数字或 高/中/低 文本。
    返回 (高/中/低, score0-100)。"""
    n = _num(confidence_level)
    if n is not None and 0 < n <= 10:
        score = int(round(n * 10))
        label = '高' if n >= 8 else ('中' if n >= 5 else '低')
        return label, score
    try:
        from enums import normalize_confidence
        return normalize_confidence(confidence_level), None
    except Exception:
        return '中', None


def _horizon_from_text(holding_period: str = '') -> Tuple[str, int]:
    t = str(holding_period or '')
    if re.search(r'日内|当日|intraday', t):
        return 'intraday', 1
    if re.search(r'短线|短期|1[-~]3|几天|数日', t):
        return 'short', 3
    if re.search(r'长线|长期|中长期|数月|半年|一年|long', t):
        return 'long', 20
    if re.search(r'波段|中线|中期|1[-~]2周|swing', t):
        return 'swing', 10
    return 'swing', 10  # 默认按波段 10 个交易日


# ── 创建 / 去重 / 反向作废 / 惰性过期 ──────────────────────────────
def _now() -> str:
    return datetime.now().isoformat()


def _lazy_expire(cur):
    """把超 expires_at 的 active 信号置 expired(惰性,在查询/创建前调)。"""
    try:
        cur.execute('''UPDATE decision_signals SET status='expired', updated_at=?
                       WHERE status='active' AND expires_at IS NOT NULL AND expires_at < ?''',
                    (_now(), _now()))
    except Exception:
        pass


def create_signal(code: str, name: str = '', action: str = 'hold',
                  source_type: str = 'manual', source_ref: str = '',
                  rating: str = '', confidence: str = '', score: Optional[int] = None,
                  horizon: str = 'swing', horizon_days: Optional[int] = None,
                  ref_price: Optional[float] = None,
                  entry_low: Optional[float] = None, entry_high: Optional[float] = None,
                  stop_loss: Optional[float] = None, target_price: Optional[float] = None,
                  reason: str = '', risk: str = '', trace_id: str = '',
                  ttl_days: Optional[int] = None) -> Tuple[Optional[int], bool]:
    """落一条决策信号。返回 (id, created)。created=False 表示命中去重返回了已有 active 信号。

    去重:同 (code, source_type, action, horizon) 已有 active 信号 → 不重复插,返回旧 id。
    反向作废:新 active 的方向与某 active 信号相反(买↔卖)→ 把旧的置 invalidated。
    """
    action = action if action in ACTIONS else 'hold'
    if action not in ACTIONS:
        action = 'hold'
    if horizon_days is None:
        horizon_days = _HORIZON_DAYS.get(horizon, 10)
    code = str(code or '').strip()
    if not code:
        return (None, False)
    try:
        _ensure_tables()
        if not _tables_ready:
            return (None, False)
        ttl = ttl_days if ttl_days is not None else max(horizon_days * 2, 5)
        expires_at = (datetime.now() + timedelta(days=ttl)).isoformat()
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        _lazy_expire(cur)
        # 去重:同来源同动作同周期的活跃信号
        cur.execute('''SELECT id FROM decision_signals
                       WHERE code=? AND source_type=? AND action=? AND horizon=? AND status='active'
                       ORDER BY id DESC''', (code, source_type, action, horizon))
        dup = cur.fetchone()
        if dup:
            conn.commit()
            conn.close()
            return (int(dup[0]), False)
        # 反向作废:方向相反的活跃信号置 invalidated
        new_dir = _ACTION_DIR.get(action, 0)
        if new_dir != 0:
            cur.execute("SELECT id, action FROM decision_signals WHERE code=? AND status='active'",
                        (code,))
            for sid, act in cur.fetchall():
                if _ACTION_DIR.get(act, 0) == -new_dir:
                    cur.execute("UPDATE decision_signals SET status='invalidated', updated_at=? WHERE id=?",
                                (_now(), sid))
        now = _now()
        cur.execute('''INSERT INTO decision_signals
            (code, name, source_type, source_ref, action, rating, confidence, score,
             horizon, horizon_days, ref_price, entry_low, entry_high, stop_loss, target_price,
             reason, risk, status, trace_id, created_at, expires_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (code, name, source_type, source_ref, action, rating, confidence, score,
             horizon, horizon_days, ref_price, entry_low, entry_high, stop_loss, target_price,
             (reason or '')[:1000], (risk or '')[:500], 'active', trace_id, now, expires_at, now))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return (int(new_id) if new_id else None, True)
    except Exception as e:
        print(f'[decision_signal] create 失败(忽略): {type(e).__name__}: {str(e)[:100]}')
        return (None, False)


def extract_from_analysis(result: Dict[str, Any], source_type: str = 'analysis',
                          source_ref: str = '') -> Optional[int]:
    """从 analyze_single_stock_for_batch 的结果抽取一条决策信号并落库。失败返回 None(不抛)。"""
    try:
        if not isinstance(result, dict) or not result.get('success'):
            return None
        info = result.get('stock_info') or {}
        dec = result.get('final_decision') or {}
        if not isinstance(dec, dict) or not dec:
            return None
        code = str(info.get('symbol') or '').strip()
        if not code:
            return None
        rating = dec.get('rating') or ''
        advice = dec.get('operation_advice') or ''
        action = normalize_action(rating, advice)
        conf_label, score = _conf_label(dec.get('confidence_level'))
        horizon, hdays = _horizon_from_text(dec.get('holding_period'))
        elo, ehi = _range(dec.get('entry_range'))
        ref_price = _num(info.get('current_price'))
        if ref_price is None:
            try:
                import datahub
                q = datahub.quote(code)
                ref_price = _num(q.get('price')) if isinstance(q, dict) else None
            except Exception:
                pass
        try:
            from enums import normalize_rating
            rating_cn = normalize_rating(rating)
        except Exception:
            rating_cn = str(rating)
        sid, _created = create_signal(
            code=code, name=info.get('name', ''), action=action,
            source_type=source_type, source_ref=str(source_ref or ''),
            rating=rating_cn, confidence=conf_label, score=score,
            horizon=horizon, horizon_days=hdays, ref_price=ref_price,
            entry_low=elo, entry_high=ehi,
            stop_loss=_num(dec.get('stop_loss')),
            target_price=_num(dec.get('take_profit')) or _num(dec.get('target_price')),
            reason=advice or dec.get('analysis_summary', ''),
            risk=dec.get('risk_warning', ''))
        return sid
    except Exception as e:
        print(f'[decision_signal] extract 失败(忽略): {type(e).__name__}: {str(e)[:100]}')
        return None


_COLS = ['id', 'code', 'name', 'source_type', 'source_ref', 'action', 'rating',
         'confidence', 'score', 'horizon', 'horizon_days', 'ref_price', 'entry_low',
         'entry_high', 'stop_loss', 'target_price', 'reason', 'risk', 'status',
         'trace_id', 'created_at', 'expires_at', 'updated_at']


def list_signals(code: Optional[str] = None, status: Optional[str] = 'active',
                 action: Optional[str] = None, source_type: Optional[str] = None,
                 days: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        _ensure_tables()
        if not _tables_ready:
            return []
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        _lazy_expire(cur)
        sql = f"SELECT {', '.join(_COLS)} FROM decision_signals WHERE 1=1"
        params: List[Any] = []
        if code:
            sql += ' AND code=?'; params.append(str(code).strip())
        if status:
            sql += ' AND status=?'; params.append(status)
        if action:
            sql += ' AND action=?'; params.append(action)
        if source_type:
            sql += ' AND source_type=?'; params.append(source_type)
        if days:
            sql += ' AND created_at >= ?'
            params.append((datetime.now() - timedelta(days=days)).isoformat())
        sql += ' ORDER BY id DESC LIMIT ?'; params.append(int(limit))
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        conn.commit()
        conn.close()
        out = [dict(zip(_COLS, r)) for r in rows]
        for s in out:
            s['action_cn'] = ACTION_CN.get(s.get('action'), s.get('action'))
        return out
    except Exception as e:
        print(f'[decision_signal] list 失败: {type(e).__name__}: {str(e)[:80]}')
        return []


def get_latest_active(code: str) -> Optional[Dict[str, Any]]:
    rows = list_signals(code=code, status='active', limit=1)
    return rows[0] if rows else None


def update_status(signal_id: int, status: str) -> bool:
    """手动改状态(closed/archived 等)。active→终态可,终态不可逆回 active。"""
    if status not in (TERMINAL_STATUS + ('active',)):
        return False
    try:
        _ensure_tables()
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        if status == 'active':
            cur.execute("SELECT status FROM decision_signals WHERE id=?", (signal_id,))
            r = cur.fetchone()
            if not r or r[0] in TERMINAL_STATUS:
                conn.close()
                return False
        cur.execute("UPDATE decision_signals SET status=?, updated_at=? WHERE id=?",
                    (status, _now(), signal_id))
        ok = cur.rowcount > 0
        conn.commit()
        conn.close()
        return ok
    except Exception:
        return False


# ── 后验校验(outcome)──────────────────────────────────────────────
def _kline_after(code: str, anchor_iso: str, horizon_days: int):
    """取 anchor 日(含)起、向后 horizon_days 根的收盘;返回 (start_close, end_close) 或 (None,None)。
    注:datahub.kline 的日期是 DataFrame 的**索引**(列只有 OHLCV),也兼容个别源把日期放列里。"""
    try:
        import datahub
        df = datahub.kline(code, '1y')
        if df is None or len(df) == 0:
            return (None, None)
        ccol = next((c for c in ('close', 'Close', '收盘') if c in df.columns), None)
        if ccol is None:
            return (None, None)
        dcol = next((c for c in ('date', 'Date', '日期') if c in df.columns), None)
        if dcol is not None:
            dates = [str(x)[:10] for x in df[dcol].tolist()]
        else:                                   # 日期在索引
            dates = [str(x)[:10] for x in df.index.tolist()]
        closes = [float(x) for x in df[ccol].tolist()]
        pairs = sorted(zip(dates, closes), key=lambda x: x[0])
        anchor = anchor_iso[:10]
        i0 = next((i for i, (d, _) in enumerate(pairs) if d >= anchor), None)
        if i0 is None:
            return (None, None)
        i1 = i0 + horizon_days
        if i1 >= len(pairs):
            return (None, None)                 # 前向 bar 不足
        return (pairs[i0][1], pairs[i1][1])
    except Exception:
        return (None, None)


def run_outcomes(days: int = 60, force: bool = False, limit: int = 500) -> Dict[str, Any]:
    """对近 days 天创建、已过 horizon 的信号做后验:用 K线判 hit/miss/neutral 并落库。
    跳过已评过的(除非 force)。返回 {evaluated, hit, miss, neutral, unable, skipped}。"""
    stat = {'evaluated': 0, 'hit': 0, 'miss': 0, 'neutral': 0, 'unable': 0, 'skipped': 0}
    try:
        _ensure_tables()
        if not _tables_ready:
            return stat
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cur.execute(f'''SELECT {', '.join(_COLS)} FROM decision_signals
                        WHERE created_at >= ? ORDER BY id DESC LIMIT ?''', (cutoff, int(limit)))
        sigs = [dict(zip(_COLS, r)) for r in cur.fetchall()]
        for s in sigs:
            sid, hdays = s['id'], int(s.get('horizon_days') or 10)
            # 已评过?
            if not force:
                cur.execute("SELECT 1 FROM decision_signal_outcomes WHERE signal_id=? AND horizon_days=?",
                            (sid, hdays))
                if cur.fetchone():
                    stat['skipped'] += 1
                    continue
            direction = _ACTION_DIR.get(s.get('action'), 0)
            start, end = _kline_after(s['code'], s.get('created_at') or '', hdays)
            if start is None or end is None or start <= 0:
                stat['unable'] += 1
                continue
            ret = (end - start) / start * 100.0
            if direction == 0:
                outcome = 'neutral'
            else:
                outcome = 'hit' if (ret * direction) > 0 else 'miss'
            stat[outcome] += 1
            stat['evaluated'] += 1
            try:
                if USE_POSTGRES:
                    cur.execute('''INSERT INTO decision_signal_outcomes
                        (signal_id,horizon_days,anchor_date,eval_date,start_price,end_price,
                         ret_pct,direction_expected,outcome,engine_version,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT (signal_id,horizon_days) DO UPDATE SET
                          end_price=EXCLUDED.end_price, ret_pct=EXCLUDED.ret_pct,
                          outcome=EXCLUDED.outcome, eval_date=EXCLUDED.eval_date''',
                        (sid, hdays, (s.get('created_at') or '')[:10], _now()[:10],
                         round(start, 3), round(end, 3), round(ret, 2), direction, outcome,
                         ENGINE_VERSION, _now()))
                else:
                    cur.execute('''INSERT OR REPLACE INTO decision_signal_outcomes
                        (signal_id,horizon_days,anchor_date,eval_date,start_price,end_price,
                         ret_pct,direction_expected,outcome,engine_version,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (sid, hdays, (s.get('created_at') or '')[:10], _now()[:10],
                         round(start, 3), round(end, 3), round(ret, 2), direction, outcome,
                         ENGINE_VERSION, _now()))
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[decision_signal] run_outcomes 失败: {type(e).__name__}: {str(e)[:80]}')
    return stat


def outcome_stats(dimension: str = 'action', days: int = 180) -> Dict[str, Any]:
    """已评信号按维度分桶胜率(只算方向性 buy/add/sell/reduce)。dimension∈action/source_type/horizon。"""
    dim_col = {'action': 's.action', 'source_type': 's.source_type',
               'horizon': 's.horizon'}.get(dimension, 's.action')
    out = {'dimension': dimension, 'days': days, 'buckets': []}
    try:
        _ensure_tables()
        if not _tables_ready:
            return out
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cur.execute(f'''SELECT {dim_col} AS dim, o.outcome, o.ret_pct
                        FROM decision_signal_outcomes o
                        JOIN decision_signals s ON s.id = o.signal_id
                        WHERE s.created_at >= ?''', (cutoff,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {str(e)[:80]}'
        return out
    agg: Dict[str, Dict[str, Any]] = {}
    for dim, outcome, ret in rows:
        b = agg.setdefault(str(dim), {'n': 0, 'hit': 0, 'miss': 0, 'neutral': 0, 'ret_sum': 0.0, 'ret_n': 0})
        b['n'] += 1
        b[outcome] = b.get(outcome, 0) + 1
        if ret is not None:
            b['ret_sum'] += float(ret); b['ret_n'] += 1
    buckets = []
    for k, v in agg.items():
        directional = v['hit'] + v['miss']
        buckets.append({
            'bucket': k, 'bucket_cn': ACTION_CN.get(k, k) if dimension == 'action' else k,
            'n': v['n'], 'hit': v['hit'], 'miss': v['miss'], 'neutral': v['neutral'],
            'win_rate_pct': round(v['hit'] / directional * 100, 1) if directional else None,
            'avg_ret_pct': round(v['ret_sum'] / v['ret_n'], 2) if v['ret_n'] else None,
        })
    out['buckets'] = sorted(buckets, key=lambda x: -x['n'])
    return out


def feedback_text(days: int = 120, min_n: int = 3) -> str:
    """给 AI prompt 注入的一行决策信号后验战绩:各动作历史方向命中率。
    让 decision_signal 从"测量环"变"反馈环"——选股/决策环节读回它,对历史 miss 率高的动作更审慎。
    无足够样本返回空串(不打扰)。"""
    try:
        st = outcome_stats('action', days=days)
        rows = [b for b in st.get('buckets', [])
                if (b.get('hit', 0) + b.get('miss', 0)) >= min_n and b.get('win_rate_pct') is not None]
        if not rows:
            return ''
        rows.sort(key=lambda x: -(x.get('hit', 0) + x.get('miss', 0)))
        parts = [f"{b.get('bucket_cn', b['bucket'])}方向命中{b['win_rate_pct']}%(n={b['hit'] + b['miss']})"
                 for b in rows[:4]]
        return '【决策信号历史后验】各动作真实方向命中率:' + '、'.join(parts) + ';命中率低的动作请更审慎。'
    except Exception:
        return ''


if __name__ == '__main__':
    import io, json
    _sys.stdout = io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== decision_signal 自检 ===')
    # 模拟一条 analyze 结果
    fake = {'success': True,
            'stock_info': {'symbol': '600519', 'name': '贵州茅台', 'current_price': 1500.0},
            'final_decision': {'rating': '买入', 'operation_advice': '逢低增持',
                               'entry_range': '1450-1480', 'take_profit': '1700',
                               'stop_loss': '1380', 'holding_period': '波段(1-2周)',
                               'confidence_level': '7', 'risk_warning': '高位回调风险'}}
    sid = extract_from_analysis(fake, source_ref='selftest')
    print('extracted signal id:', sid)
    print('latest active 600519:', json.dumps(get_latest_active('600519'), ensure_ascii=False)[:300])
    print('run_outcomes:', run_outcomes(days=1))
    print('stats:', json.dumps(outcome_stats('action', 1), ensure_ascii=False)[:200])
