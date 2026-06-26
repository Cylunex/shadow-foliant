import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导（搬入子目录后定位项目根）
"""
Jobs Hub — 统一的后台任务注册与管理

借鉴 instock 的 Job 设计思路：
  - 后台预算重型数据（指标、北向、龙虎榜），存入快照表
  - Agent 分析时直接读快照，避免每次重算
  - 复用现有 `schedule` 库（不引入 APScheduler，避免破坏既有 4 个 scheduler）

⚠️ 时区说明：系统时区为 Asia/Shanghai (CST, UTC+8)，所有时间和 register() 参数直接使用 CST。

用法：
    from jobs_hub import hub
    hub.register('daily_market_snapshot', '15:50', task_daily_market_snapshot)
    hub.start()
    # ...
    hub.stop()
"""

import os
import sys
import json
import math
import sqlite3
import threading
import time
import concurrent.futures

# DB 路由（USE_POSTGRES=true 时走 PG，否则用 SQLite）
from db_compat import connect as db_connect, USE_POSTGRES
from datetime import datetime, date
from typing import Callable, Dict, List, Optional

import schedule
import datahub  # 统一外部数据层(行情/北向/龙虎榜/板块/新闻等取数唯一入口)


# =============================================================================
# 快照存储 — SQLite，PG 切换沿用 USE_POSTGRES
# =============================================================================

_SNAPSHOT_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')

# _log_run 高频调用，缓存 SQLite 连接复用（线程安全锁保护）
_LOG_DB_LOCK = threading.Lock()
_LOG_DB_CACHE: dict = {'conn': None, 'pid': None}


def _get_log_db():
    """获取缓存后的 SQLite 连接（断线自动重连），仅用于 _log_run"""
    with _LOG_DB_LOCK:
        pid = os.getpid()
        cache = _LOG_DB_CACHE
        if cache['conn'] is not None and cache['pid'] == pid:
            try:
                cache['conn'].execute('SELECT 1')
                return cache['conn']
            except Exception:
                pass
        conn = db_connect(_SNAPSHOT_DB_PATH)
        from db_compat import USE_POSTGRES
        if not USE_POSTGRES:
            conn.execute('PRAGMA journal_mode=WAL')
        cache['conn'] = conn
        cache['pid'] = pid
        return conn


def _init_snapshot_db():
    """初始化快照表 — PG 模式下表已通过 scripts/init_postgres.sql 建好，跳过"""
    if USE_POSTGRES:
        return
    conn = db_connect(_SNAPSHOT_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS indicator_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            indicators TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, snapshot_date)
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            error TEXT
        )
    ''')
    conn.commit()
    conn.close()


_init_snapshot_db()


def _json_sanitize(obj):
    """递归把 NaN/Inf → None。PG 的 JSONB 列拒收 NaN/Infinity 字面量,而 json.dumps 默认
    (allow_nan=True)会把 float('nan') 原样写成 NaN → 写库报 `invalid input syntax for type
    json: Token "NaN" is invalid`(SQLite 不校验 JSON 所以本地发现不了)。datahub 行情/北向
    记录里常有 NaN(如 turnover_ratio),写快照前必须洗。numpy.float64 是 float 子类,
    isnan/isinf 通吃;其余 numpy 标量随后由 json.dumps(default=str) 兜底。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return obj


def save_indicator_snapshot(symbol: str, indicators: Dict):
    """保存某只股票当日指标快照（重复触发会覆盖当天的）"""
    today = datetime.now().strftime('%Y-%m-%d')
    conn = db_connect(_SNAPSHOT_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO indicator_snapshots(symbol, snapshot_date, indicators)
        VALUES (?, ?, ?)
        ON CONFLICT(symbol, snapshot_date) DO UPDATE SET
            indicators = excluded.indicators,
            created_at = CURRENT_TIMESTAMP
    ''', (symbol, today, json.dumps(_json_sanitize(indicators), ensure_ascii=False, default=str)))
    conn.commit()
    conn.close()


def _coerce_json(value):
    """PG JSONB 已是 dict/list；SQLite TEXT 需要 json.loads"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def get_indicator_snapshot(symbol: str, date: str = None) -> Optional[Dict]:
    """读取快照；date=None 则取当日，当日无数据则回退到最近交易日"""
    date = date or datetime.now().strftime('%Y-%m-%d')
    conn = db_connect(_SNAPSHOT_DB_PATH)
    cur = conn.cursor()
    # PG ⚠️ 表存在但 indicator_snapshots 会有数据。
    sql = 'SELECT indicators FROM indicator_snapshots WHERE symbol=? AND snapshot_date=? LIMIT 1'
    cur.execute(sql, (symbol, date))
    row = cur.fetchone()
    if row:
        conn.close()
        return _coerce_json(row[0])
    # 当日无数据 → 回退到最近一条
    cur.execute(
        'SELECT indicators FROM indicator_snapshots WHERE symbol=? ORDER BY snapshot_date DESC LIMIT 1',
        (symbol,),
    )
    row = cur.fetchone()
    conn.close()
    return _coerce_json(row[0]) if row else None


def _last_selection_picks() -> List[str]:
    """读今日综合选股(unified_selection 09:45)的 TOP 代码列表,统一收口。

    ⚠️ 关键坑:`save_indicator_snapshot('_last_selection', {'picks':[...]})` 把 dict 直接存进
    indicators 列,`get_indicator_snapshot` 返回的就是该列反序列化结果 = {'picks':[...]} 本身,
    **没有 'indicators' 外层包裹**。历史上多处消费者误写 `snap.get('indicators')` → 恒 None →
    死分支(盘后扫描并入/妙想复核 当日选股 都因此恒空)。一律走本函数取 picks,别再裸读。"""
    try:
        snap = get_indicator_snapshot('_last_selection')
        if isinstance(snap, dict):
            picks = snap.get('picks')
            return [str(c).strip() for c in picks if c] if isinstance(picks, list) else []
        if isinstance(snap, list):
            return [str(c).strip() for c in snap if c]
    except Exception:
        pass
    return []


def save_market_snapshot(payload: Dict):
    today = datetime.now().strftime('%Y-%m-%d')
    conn = db_connect(_SNAPSHOT_DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO market_snapshots(snapshot_date, payload)
        VALUES (?, ?)
        ON CONFLICT(snapshot_date) DO UPDATE SET
            payload = excluded.payload,
            created_at = CURRENT_TIMESTAMP
    ''', (today, json.dumps(_json_sanitize(payload), ensure_ascii=False, default=str)))
    conn.commit()
    conn.close()


def get_market_snapshot(date: str = None) -> Optional[Dict]:
    date = date or datetime.now().strftime('%Y-%m-%d')
    conn = db_connect(_SNAPSHOT_DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT payload FROM market_snapshots WHERE snapshot_date=?', (date,))
    row = cur.fetchone()
    conn.close()
    return _coerce_json(row[0]) if row else None


def _log_run(job_name: str, status: str, error: str = None,
             started_at: str = None, finished_at: str = None, notify: bool = True):
    # 整个 jobs_hub 的可观测性靠这一个 DB 写入点,它被成功/失败/跳过三条路径调用。
    # 必须设防:SQLite 写锁(database is locked)或连接失效时,遥测写失败绝不能击穿任务执行/调度
    # (尤其 except 路径与 _skip_if_not_trading 内的调用,二次抛出会丢日志/让静默跳过变异常)。
    try:
        conn = _get_log_db()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO job_runs(job_name, started_at, finished_at, status, error)
            VALUES (?, ?, ?, ?, ?)
        ''', (job_name, started_at or datetime.now().isoformat(), finished_at, status, error))
        conn.commit()
        try:
            conn.close()
        except Exception:
            pass
    except Exception as _le:
        print(f'[_log_run] 写 job_runs 失败(忽略,不阻断任务): {type(_le).__name__}: {str(_le)[:80]}',
              flush=True)
    # 任务失败自动推送告警(本身已设防,不让推送失败再冒泡)。
    # notify=False:调用方会自己发更贴切的通知(超时→平和的 _notify_data_unavailable;
    # 真异常→带 traceback 的 _notify_task_error),此处不再重复推一条吓人的「⚠️任务异常」
    # (否则同一次失败双推送,违背"超时只发平和提示"的约定)。任务内部自处理错误并直接调
    # _log_run('error') 的路径仍默认推送,保证那条是唯一告警。
    if status == 'error' and error and notify:
        try:
            _push_error(f'⚠️ 任务异常: {job_name}', f'{error[:500]}')
        except Exception:
            pass


# --- 交易日历（节假日感知） ---
# 用 akshare 官方 A 股交易日历;进程内按"加载日"缓存,一天最多拉一次。
# 联网/akshare 失败、或查询日期超出日历覆盖范围时,回退到"只跳周六/日"(绝不误杀真实交易日)。
_TRADE_CAL_LOCK = threading.Lock()
_TRADE_CAL = {'dates': None, 'min': None, 'max': None, 'loaded_on': None}


def _load_trade_calendar():
    """加载/刷新交易日历到进程缓存(每个自然日最多一次)。失败则保持 dates=None → 回退周末判断。"""
    today = datetime.now().date()
    if _TRADE_CAL['loaded_on'] == today and _TRADE_CAL['dates'] is not None:
        return
    with _TRADE_CAL_LOCK:
        if _TRADE_CAL['loaded_on'] == today and _TRADE_CAL['dates'] is not None:
            return
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            ds = {v if isinstance(v, date) else datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
                  for v in df['trade_date'].tolist()}
            _TRADE_CAL.update(dates=ds, min=min(ds), max=max(ds), loaded_on=today)
        except Exception as e:
            _TRADE_CAL['loaded_on'] = today  # 标记今天已尝试,避免反复重试拖慢任务
            print(f'[jobs_hub] 交易日历加载失败,本日回退到"只判周末": {e}')


def _is_trading_day(d: datetime = None) -> bool:
    """判断是否为 A 股交易日(节假日感知)。
    优先用 akshare 官方交易日历;日历不可用或日期超出覆盖范围时,回退到"只跳周六/日"。"""
    dt = d or datetime.now()
    day = dt.date() if isinstance(dt, datetime) else dt  # datetime 是 date 子类,纯 date 走 else
    _load_trade_calendar()
    cal = _TRADE_CAL['dates']
    if cal and _TRADE_CAL['min'] <= day <= _TRADE_CAL['max']:
        return day in cal
    return day.weekday() < 5  # 回退:无日历或超出日历范围(0=Mon..6=Sun)


def _skip_if_not_trading(job_name: str) -> bool:
    """非交易日跳过；记录 skipped"""
    if not _is_trading_day():
        _log_run(job_name, 'skipped', error='non-trading day',
                 started_at=datetime.now().isoformat(),
                 finished_at=datetime.now().isoformat())
        return True
    return False


# =============================================================================
# 预置 Job 任务函数
# =============================================================================

def task_portfolio_indicator_snapshot():
    """对所有持仓+监测列表股票预算 MyTT 指标并存快照（盘后跑）。

    已并入(2026-06-12 任务整合):
      - 原 morning_warmup:监测列表股票一并算快照(盘后算好,次日盘前直接读,无需再预热)
      - 原 wf_portfolio_risk:VaR/回撤聚合预警(复用本轮已算数据,零额外取数)
      - 原 wf_daily_pattern_alert:TA-Lib 反转形态(复用同一 df) + 基本面E级(开关 wf_daily_pattern_alert)
    """
    job = 'portfolio_indicator_snapshot'
    if _skip_if_not_trading(job):
        return
    _wait_kline_prefetch(job)       # F: 等 kline_prefetch 焐完缓存再逐只算快照(读暖缓存,不冷拉)
    started = datetime.now().isoformat()
    try:
        from portfolio_db import portfolio_db
        from stock_data import StockDataFetcher

        # 持仓 + 监测列表（原 morning_warmup 的覆盖范围）
        targets = {}  # sym -> name
        stocks = portfolio_db.get_all_stocks() if hasattr(portfolio_db, 'get_all_stocks') else []
        for s in stocks:
            sym = s.get('code') if isinstance(s, dict) else s
            if sym:
                targets[str(sym)] = (s.get('name', '') if isinstance(s, dict) else '')
        holding_syms = set(targets)
        try:
            from monitor_db import monitor_db
            for s in monitor_db.get_monitored_stocks() or []:
                sym = s.get('symbol') if isinstance(s, dict) else None
                if sym and str(sym) not in targets:
                    targets[str(sym)] = s.get('name', '')
        except Exception:
            pass
        if not targets:
            _log_run(job, 'skipped', error='no portfolio/monitor stocks', started_at=started,
                     finished_at=datetime.now().isoformat())
            return

        # 形态告警开关（重操作:逐只 TA-Lib + 基本面打分）
        pattern_on = False
        try:
            from automation_config import is_enabled
            pattern_on = is_enabled('wf_daily_pattern_alert')
        except Exception:
            pass
        REVERSAL_HIGH = {
            'morning_star', 'morning_doji_star', 'evening_star', 'evening_doji_star',
            'abandoned_baby_bull', 'abandoned_baby_bear',
            'three_white_soldiers', 'three_black_crows',
            'engulfing_bull', 'engulfing_bear',
            'hammer', 'shooting_star', 'hanging_man', 'inverted_hammer',
        }
        _pat_det = None
        if pattern_on:
            try:
                from pattern_recognition import PatternDetector
                _pat_det = PatternDetector()
            except Exception:
                _pat_det = None

        # ⭐ 2026-06-18: portfolio_snapshot 提到主循环之前先落地
        # 之前在循环末尾(line 366), 任何一只股票卡死都会让 save_snapshot 不执行
        # → stock_portfolio_snapshots 当日缺 row → 22:30 daily_pnl 推送显示"股票 0 元(0 只)"。
        # save_snapshot 本身只读 quotes 算市值, 跟下面 K 线大循环无依赖, 提前到这里成本 0。
        try:
            import portfolio_snapshot as _ps
            _ps.save_snapshot(datetime.now().strftime('%Y-%m-%d'))
        except Exception as e:
            print(f'[portfolio_indicator_snapshot] save_snapshot 失败: {type(e).__name__}: {str(e)[:80]}')

        fetcher = StockDataFetcher()
        ok, fail = 0, 0
        risk_rows = []      # (name, sym, var95, mdd) 仅持仓
        pattern_alerts = []
        for sym, name in targets.items():
            try:
                df = fetcher.get_stock_data(sym, '6mo', adjust='qfq')  # 指标快照+缠论用前复权
                if isinstance(df, dict) and df.get('error'):
                    fail += 1
                    continue
                df_ind = fetcher.calculate_technical_indicators(df)
                if isinstance(df_ind, dict) and df_ind.get('error'):
                    fail += 1
                    continue
                latest = fetcher.get_latest_indicators(df_ind)
                # 加强(借鉴新模块):缠论买卖点 + 量化风险(VaR/最大回撤),并入快照(JSON blob,安全)
                try:
                    from chan_theory import analyze_chan
                    ch = analyze_chan(df, sym)
                    if isinstance(ch, dict) and ch.get('available'):
                        latest['chan_signal'] = (ch.get('buy_sell_point') or {}).get('signal')
                        latest['chan_direction'] = ch.get('current_direction')
                except Exception:
                    pass
                try:
                    from stress_testing import analyze_risk
                    rk = analyze_risk(df)
                    if isinstance(rk, dict) and rk.get('available'):
                        latest['var95'] = rk.get('var_hist')
                        latest['max_drawdown'] = (rk.get('max_drawdown') or {}).get('max_drawdown')
                        if sym in holding_syms:
                            risk_rows.append((name or sym, sym, latest['var95'], latest['max_drawdown']))
                except Exception:
                    pass
                # 反转形态扫描（原 wf_daily_pattern_alert,复用同一 df,零额外取数）
                if _pat_det is not None and sym in holding_syms:
                    try:
                        results = _pat_det.detect_all(df, lookback=2)
                        hits = []
                        for pid, r in results.items():
                            if pid == 'support_resistance' or not isinstance(r, dict):
                                continue
                            if r.get('found') and pid in REVERSAL_HIGH and r.get('days_ago', 99) <= 1:
                                hits.append(f"{r.get('type', '')} {r.get('name', pid)}")
                        if hits:
                            pattern_alerts.append(f"⚡ {sym} {name}: {' | '.join(hits)}")
                    except Exception:
                        pass
                save_indicator_snapshot(sym, latest)
                ok += 1
            except Exception:
                fail += 1
        # 2026-06-18: save_snapshot 已挪到循环开头先执行(防循环卡死导致当日 row 不写),
        # 这里不再重复调。

        # ── 风险聚合预警（原 wf_portfolio_risk）:VaR95>5% 或 最大回撤<-40% ──
        try:
            risk_alerts = [
                f"⚠️ {n}({c}): VaR95 {v*100:.1f}% / 最大回撤 {(d or 0)*100:.0f}%"
                for n, c, v, d in risk_rows
                if (v is not None and v > 0.05) or (d is not None and d < -0.40)
            ]
            if risk_alerts:
                risk_rows.sort(key=lambda x: (x[2] or 0), reverse=True)
                top = '\n'.join(f"{n}({c}): VaR95 {(v or 0)*100:.1f}% 回撤 {(d or 0)*100:.0f}%"
                                for n, c, v, d in risk_rows[:10])
                _push_error('🛡️ 持仓量化风险预警',
                            f"持仓量化风险 — {datetime.now().strftime('%Y-%m-%d')}\n"
                            f"持仓 {len(holding_syms)} 只\n\n━━ 高风险预警 ━━\n"
                            + '\n'.join(risk_alerts) + '\n\nVaR 排名(Top10):\n' + top)
        except Exception:
            pass

        # ── 形态/基本面告警（原 wf_daily_pattern_alert,开关控制）──
        if pattern_on:
            e_grade_alerts = []
            try:
                from fundamental_scoring import score_one
                for sym in holding_syms:
                    try:
                        fv = score_one(sym) or {}
                        if (fv.get('grade') or '')[:1] == 'E' and not fv.get('low_coverage'):
                            e_grade_alerts.append(
                                f"❌ {sym} {targets.get(sym, '')}: 基本面 E 级 (score={fv.get('score')}) → 建议清仓评估")
                    except Exception:
                        continue
            except Exception:
                pass
            if pattern_alerts or e_grade_alerts:
                try:
                    from notification_router import send
                    body_lines = [f'📈 持仓预警 — {datetime.now().strftime("%Y-%m-%d %H:%M")}', '']
                    if pattern_alerts:
                        body_lines.append('━━━ 反转形态告警 ━━━')
                        body_lines.extend(pattern_alerts)
                    if e_grade_alerts:
                        body_lines.append('\n━━━ 基本面 E 级告警 ━━━')
                        body_lines.extend(e_grade_alerts)
                    send('alert', '⚡ 持仓预警', '\n'.join(body_lines))
                except Exception:
                    pass

        status = 'error' if fail > ok and ok == 0 else 'success'
        _log_run(job, status, error=f'ok={ok} fail={fail} risk_alerts={len(risk_rows)} patterns={len(pattern_alerts)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def _intraday_plunge_check(drop_pct: float = -5.0):
    """持仓盘中急跌监控(挂在 stock_monitor_check 每30分钟):
    批量行情扫持仓,跌幅 ≤ drop_pct 即推告警;用快照表做"每股每日只报一次"去重。
    零K线接口,只一组批量行情。"""
    holdings = _holdings_codes()
    if not holdings:
        return
    codes = [c for c, _ in holdings]
    quotes = {}
    try:
        for i in range(0, len(codes), 20):
            quotes.update(datahub.quotes(codes[i:i + 20]) or {})
    except Exception:
        return

    today = datetime.now().strftime('%Y-%m-%d')
    alerted = set()
    try:
        snap = get_indicator_snapshot('_plunge_alerted') or {}
        if snap.get('date') == today:
            alerted = set(snap.get('codes') or [])
    except Exception:
        pass

    hits = []
    for code, name in holdings:
        q = quotes.get(code) or {}
        try:
            chg = float(q.get('change_pct') or 0)
        except (TypeError, ValueError):
            continue
        if chg <= drop_pct and code not in alerted:
            hits.append((code, q.get('name') or name, chg, q.get('price')))
            alerted.add(code)

    if not hits:
        return
    hits.sort(key=lambda x: x[2])
    lines = [f'🚨 持仓盘中急跌 — {datetime.now().strftime("%H:%M")}', '']
    for code, name, chg, price in hits:
        lines.append(f'  • {name} {code}  {chg:+.1f}%' + (f'  ¥{price}' if price else ''))
    lines.append('')
    lines.append('(每股每日仅提醒一次;详情看尾盘持仓分析)')
    _push_error('🚨 持仓急跌提醒', '\n'.join(lines))
    try:
        save_indicator_snapshot('_plunge_alerted', {'date': today, 'codes': sorted(alerted)})
    except Exception:
        pass


def _wait_kline_prefetch(job: str, max_wait: int = 300, poll: int = 10) -> bool:
    """F(盘后链依赖显式化):盘后"读暖缓存"的任务(因子采集/持仓快照/后验)开头调用,**等当日
    kline_prefetch 焐完缓存再继续** —— 把"靠 16:30→16:40→16:45 时钟间隔保证 prefetch 先跑"的
    隐式依赖,变成显式 barrier。根治 prefetch 跑超时(东财封)时下游在缓存没焐好就冷拉。

    每个任务在独立 worker 线程跑(线程池 6),此处 sleep 不阻塞调度线程,只占一个 worker。
    返回 True=缓存视为已暖(prefetch 当日已结束) / False=等超时或无法判定 → **fail-open 照常继续**
    (绝不因依赖检查误杀盘后任务;DB 读不了/prefetch 没开 一律放行)。"""
    try:
        from automation_config import is_enabled
        if not is_enabled('kline_prefetch'):
            return False   # prefetch 没开 → 无可等,直接放行
    except Exception:
        pass
    waited = 0
    while True:
        try:
            conn = db_connect(_SNAPSHOT_DB_PATH)
            cur = conn.cursor()
            # ⚠️ PG 里 started_at 是 timestamptz,不能 LIKE(会 operator 报错);按"当日"过滤分库:
            #    PG 用 ::date = CURRENT_DATE、SQLite 用 DATE()(started_at 为 ISO 文本)。
            if USE_POSTGRES:
                cur.execute("""SELECT finished_at FROM job_runs
                               WHERE job_name='kline_prefetch' AND started_at::date = CURRENT_DATE
                               ORDER BY id DESC LIMIT 1""")
            else:
                cur.execute("""SELECT finished_at FROM job_runs
                               WHERE job_name='kline_prefetch' AND DATE(started_at) = DATE('now')
                               ORDER BY id DESC LIMIT 1""")
            row = cur.fetchone()
            conn.close()
        except Exception:
            return False   # 读不了 job_runs → fail-open
        if row and row[0]:          # 当日已有 finished 记录 → 缓存已暖
            return True
        if waited >= max_wait:      # 等够仍未完(还在跑/未起) → fail-open 继续
            print(f'[{job}] 等 kline_prefetch 焐缓存超 {max_wait}s 仍未结束,照常继续'
                  f'(prefetch 慢/未跑?datahub 已有全源熔断兜底)', flush=True)
            return False
        time.sleep(poll)
        waited += poll


def task_daily_backtest():
    """盘后批量回测 + 策略基因组进化：全股池回测 → 横截面评分 → 变异 → 反哺AI。

    每天 16:30 运行。受开关 daily_backtest 控制（默认开）。

    v2 进化版：
      1. 池子：持仓 + 昨日强势股 TOP20 → 扩容到 30+
      2. 策略：取所有活跃变体（含默认 + 新生变异体）
      3. 回测后：横截面聚合 → strategy_scores，更新 variant fitness
      4. 个股适配度更新 → stock_strategy_affinity
      5. 触发进化：突变 + 交叉 → 新变体入库
      6. 推送进化日报（替换旧纯文本）
    """
    job = 'daily_backtest'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        from portfolio_db import portfolio_db
        from backtest_engine import backtest_one
        from stock_data import StockDataFetcher
        from analysis.strategy_genome import (
            init_genome_tables, seed_default_variants,
            get_variants_for_eval, save_strategy_score,
            update_variant_fitness, update_stock_affinity,
            evolve_generation, compute_strategy_score,
            build_evolution_report, default_params,
            coerce_params, STRATEGY_PARAM_SPACE,
            FIXED_STRATEGIES, evolve_composed, ensure_composed_population,
        )

        # 确保表 + 种子就绪(含组合策略种群保底)
        init_genome_tables()
        seed_default_variants()
        try:
            ensure_composed_population(min_active=12)
        except Exception:
            pass

        fetcher = StockDataFetcher()

        # ── 0b. 市场环境(牛/熊/震荡)→ 写入 strategy_scores.market_regime ──
        # (2026-06-12:该列建表时就有但从来没写过;突破类策略牛熊效能天差地别,评分必须带环境)
        market_regime = None
        for _idx_code in ('000300', '510300'):  # 沪深300指数,不行用300ETF
            try:
                _idx_df = fetcher.get_stock_data(_idx_code, '1y')
                if _idx_df is not None and not isinstance(_idx_df, dict) and len(_idx_df) > 60:
                    from strategy_signals import detect_regime
                    market_regime = detect_regime(_idx_df)
                    break
            except Exception:
                continue

        # ── 1. 构建股票池：持仓 + 强势股 TOP20（去重） ──
        pool = {}
        stocks = portfolio_db.get_all_stocks() if hasattr(portfolio_db, 'get_all_stocks') else []
        for s in stocks:
            code = s.get('code', '')
            if code and code not in pool:
                pool[code] = s.get('name', '')

        # 尝试获取昨日强势股作为补充
        try:
            from selection.strong_stock import get_top_strong_stocks
            strong = get_top_strong_stocks(top_n=20)
            for code, name in (strong or []):
                if code and code not in pool:
                    pool[code] = name
        except Exception:
            pass

        if not pool:
            _log_run(job, 'success', error='empty pool',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        # ── 2. 获取数据，筛出有数据的股票 ──
        valid_stocks = []  # [(code, name, df)]
        for code, name in pool.items():
            try:
                df = fetcher.get_stock_data(code, "2y", adjust='qfq')  # 策略形态扫描用前复权
                if df is not None and not isinstance(df, dict) and hasattr(df, '__len__') and len(df) > 60:
                    valid_stocks.append((code, name, df))
            except Exception:
                continue

        n_pool = len(valid_stocks)
        if n_pool == 0:
            _log_run(job, 'success', error='no valid stock data',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        # ── 3. 获取本轮要评估的变体:每策略 高分top5 + 未评估新生5;组合策略 top6+新生10 ──
        # (修复:原全局 LIMIT 100 按分排,新生变体 NULLS LAST 永远轮不到评估)
        all_variants = []
        for _sid in STRATEGY_PARAM_SPACE:
            try:
                all_variants.extend(get_variants_for_eval(_sid, top_n=5, fresh_n=5))
            except Exception:
                continue
        try:
            all_variants.extend(get_variants_for_eval('composed', top_n=6, fresh_n=10))
        except Exception:
            pass
        if not all_variants:
            _log_run(job, 'success', error='no active variants',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        # ── 4. 批量回测 ──
        # 结构: {strategy_id: {variant_id, params, results: [{code, name, win_rate, avg_ret, ...}]}}
        strategy_results = {}
        for v in all_variants:
            sid = v['base_strategy']
            vid = v['id']
            params = v['params'] if isinstance(v['params'], dict) else json.loads(v['params'])
            key = f"{sid}:{vid}"
            strategy_results[key] = {
                'strategy_id': sid, 'variant_id': vid, 'params': params,
                'results': [],
            }

        # 样本外切分:近 _HOLDOUT_DAYS 天的触发为 holdout(out-of-sample),其余为 train。
        # 单次回测拿全样本 trades,再按 trigger_date 切两段(不翻倍回测)。train 驱动适应度/进化,
        # holdout 入库供 get_live_strategy_set 的部署门(只有样本外也成立的变体才上线)。
        from datetime import timedelta
        _HOLDOUT_DAYS = 120
        _split_date = (datetime.now() - timedelta(days=_HOLDOUT_DAYS)).strftime('%Y-%m-%d')

        def _agg_trades(ts):
            """从 trades 子集算 (win_rate%, avg_ret%, count)。"""
            if not ts:
                return (0.0, 0.0, 0)
            rets = [t.get('ret_pct', 0) or 0 for t in ts]
            wr = sum(1 for x in rets if x > 0) / len(rets) * 100
            return (round(wr, 1), round(sum(rets) / len(rets), 2), len(rets))

        # N² 回测(股 × 变体)是全任务最重的一段,纯 CPU(pandas/numpy 在内存 df 上算)。
        # 用**进程池按股并行**(每 worker 跑该股全部变体):结果与串行逐位一致,实测多核 ~Nx 提速,
        # 把原 45~60min 压到个位数分钟、不再踩 deadline。受限环境(无法 fork/spawn)自动回退串行。
        total_backtests = 0
        _bt_tasks = [(code, name, df, all_variants, _split_date, n_pool)
                     for code, name, df in valid_stocks]
        _per_stock = None
        _workload = len(valid_stocks) * max(1, len(all_variants))
        if len(valid_stocks) > 1 and _workload >= 200:
            try:
                import os as _os4
                from concurrent.futures import ProcessPoolExecutor
                from genome_bt_worker import run_stock as _gbt
                _nw = min(8, len(valid_stocks), max(1, (_os4.cpu_count() or 4) - 1))
                # 整体超时:单 worker 在 numpy/pandas 卡死时,map 无 timeout 会无限阻塞 →
                # 只能等 deadman 90min 硬杀(连累并发任务)。给整体上限,超时则回退串行收尾。
                _bt_timeout = max(600, 8 * len(_bt_tasks))   # 每股~8s 上限,至少 10min
                with ProcessPoolExecutor(max_workers=_nw) as _ex:
                    _per_stock = list(_ex.map(_gbt, _bt_tasks, timeout=_bt_timeout))
                print(f'[daily_backtest] 进程池并行回测: {len(valid_stocks)}股 × {len(all_variants)}变体, {_nw} 进程')
            except Exception as _pe:
                print(f'[daily_backtest] 进程池不可用/超时,回退串行: {type(_pe).__name__}: {str(_pe)[:60]}')
                _per_stock = None
        if _per_stock is None:
            from genome_bt_worker import run_stock as _gbt
            _per_stock = [_gbt(t) for t in _bt_tasks]
        for _stock_out in _per_stock:
            for key, res in (_stock_out or []):
                if key in strategy_results:
                    strategy_results[key]['results'].append(res)
                    total_backtests += 1

        # ── 5. 横截面聚合 & 入库 ──
        # 变体适应度逐个更新;strategy_scores/个股适配 每策略只写"最优变体"的结果
        # (修复:原对同策略多变体逐个 upsert 同一行(strategy_id,eval_date) → 后写覆盖先写,
        #  留在表里的是"最后迭代到的"而非"最好的"变体)
        per_strategy_best = {}  # sid -> {'score','sr','agg'}
        for key, sr in strategy_results.items():
            results = sr['results']
            if not results:
                continue
            n_triggered = sum(1 for r in results if r['trigger_count'] > 0)

            # 聚合统计
            wr_all = [r['win_rate'] for r in results if r['trigger_count'] > 0]
            ar_all = [r['avg_ret'] for r in results if r['trigger_count'] > 0]
            dd_all = [r['max_dd'] for r in results]
            best_all = [r['best_ret'] for r in results if r['trigger_count'] > 0]
            worst_all = [r['worst_ret'] for r in results if r['trigger_count'] > 0]

            avg_wr = sum(wr_all) / len(wr_all) if wr_all else 0
            avg_ar = sum(ar_all) / len(ar_all) if ar_all else 0
            avg_dd = sum(dd_all) / len(dd_all) if dd_all else 0
            best_ret = max(best_all) if best_all else 0
            worst_ret = min(worst_all) if worst_all else 0

            # 样本外聚合(只统计 holdout 有触发的股票)
            ho_wr_all = [r['ho_wr'] for r in results if r.get('ho_n', 0) > 0]
            ho_ar_all = [r['ho_ar'] for r in results if r.get('ho_n', 0) > 0]
            ho_trig = sum(r.get('ho_n', 0) for r in results)
            ho_avg_wr = round(sum(ho_wr_all) / len(ho_wr_all), 1) if ho_wr_all else None
            ho_avg_ar = round(sum(ho_ar_all) / len(ho_ar_all), 2) if ho_ar_all else None

            # 更新变体适应度(零触发也更新:trigger_count=0 让它有低分,可被淘汰,而非永远 NULL 待评估)
            update_variant_fitness(
                sr['variant_id'], avg_wr, avg_ar, avg_dd,
                trigger_count=n_triggered, sample_stocks=len(results),
                holdout_win_rate=ho_avg_wr, holdout_avg_ret=ho_avg_ar, holdout_trigger=ho_trig,
            )
            if n_triggered == 0:
                continue

            v_score = compute_strategy_score(avg_wr, avg_ar, n_triggered,
                                             max_trigger=n_pool, sample_stocks=len(results))
            prev = per_strategy_best.get(sr['strategy_id'])
            if prev is None or v_score > prev['score']:
                per_strategy_best[sr['strategy_id']] = {
                    'score': v_score, 'sr': sr,
                    'agg': (n_triggered, avg_wr, avg_ar, avg_dd, best_ret, worst_ret, len(results)),
                }

        for sid_best, info in per_strategy_best.items():
            sr = info['sr']
            n_triggered, avg_wr, avg_ar, avg_dd, best_ret, worst_ret, n_samples = info['agg']
            save_strategy_score(
                sid_best, sr['variant_id'],
                stock_pool_n=n_pool, triggered_n=n_triggered,
                win_rate=avg_wr, avg_ret=avg_ar, max_dd=avg_dd,
                best_ret=best_ret, worst_ret=worst_ret,
                sample_stocks=n_samples,
                market_regime=market_regime,
            )
            # 个股适配度(用最优变体的结果)
            for r in sr['results']:
                if r['trigger_count'] > 0:
                    update_stock_affinity(
                        r['code'], sid_best,
                        r['win_rate'], r['avg_ret'],
                        r['trigger_count'], r['score'],
                    )

        # ── 6. 三层进化(2026-06-12 重构) ──
        #   FIXED(经典4套): 只小步微调,变体少,保稳定
        #   DYNAMIC(其余9套): 大步动态调整(原列表漏了 climax_limitdown/high_tight_flag,现全覆盖)
        #   COMPOSED: 条件积木组合产出全新策略(结构进化+随机新血)
        new_variant_count = 0
        for sid in STRATEGY_PARAM_SPACE:
            try:
                if sid in FIXED_STRATEGIES:
                    new_ids = evolve_generation(sid, population_size=10, elites=2,
                                                mutants=4, mutation_strength=0.10, keep_top=15)
                else:
                    new_ids = evolve_generation(sid, population_size=20, elites=3,
                                                mutants=10, mutation_strength=0.30, keep_top=30)
                new_variant_count += len(new_ids)
            except Exception:
                continue
        try:
            new_variant_count += len(evolve_composed(mutants=8, randoms=4, keep_top=40))
        except Exception as ce:
            print(f'[daily_backtest] 组合策略进化失败: {ce}')

        # ── 7. 推送进化日报 ──
        report = build_evolution_report()
        from notification_router import send
        send('report', '🧬 策略进化日报', report)

        _log_run(job, 'success',
                error=(f'pool={n_pool} backtests={total_backtests} '
                       f'variants={len(all_variants)} new_variants={new_variant_count}'),
                started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())

    # ── 8. 策略命中扫描 → AI 深析 → 推荐池（原 wf_daily_strategy_scan,开关控制,默认开）──
    # 放在进化之后:扫描时注入的是刚更新的基因组情报
    try:
        _daily_strategy_scan()
    except Exception as e:
        print(f'[daily_backtest] 策略扫描子任务失败: {e}')

    # ── 9. 预热因子IC评估(写 webui 同缓存键 factor_eval:10,genome页秒读)──
    try:
        from analysis.factor_eval import evaluate as _fe
        rep = _fe(horizon=10)
        if rep.get('factors'):
            from cache import cache_set
            cache_set('factor_eval:10', rep, 86400)
            print(f"[daily_backtest] 因子IC评估预热: {len(rep['factors'])}因子")
    except Exception as e:
        print(f'[daily_backtest] 因子评估预热失败: {e}')


def task_ai_eval_weekly():
    """每周一 09:30：对过去 30 天 AI 推荐做评估，推送报告（开关 ai_eval_weekly，默认开）"""
    job = 'ai_eval_weekly'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled by automation_config',
                     started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    started = datetime.now().isoformat()
    try:
        from ai_evaluation import (evaluate_by_source, evaluate_all, format_report,
                                    format_unowned_picks, evaluate_by, format_buckets)
        overall = evaluate_all(days=30)
        by_src = evaluate_by_source(days=30)
        report_text = (
            f'📊 AI 推荐评估周报 — {datetime.now().strftime("%Y-%m-%d")}\n'
            f'\n样本: {overall.sample_size}  综合得分: {overall.score}  等级: {overall.grade}\n'
            f'\n{format_report(by_src)}\n'
        )

        # ⭐ 维度分桶:信心度 / 持有周期 — 看"高信心是否真更准""短线 vs 中长线哪个赚"
        try:
            for _dim in ('confidence', 'horizon'):
                report_text += '\n' + format_buckets(evaluate_by(_dim, days=30), _dim) + '\n'
        except Exception as de:
            print(f'[ai_eval_weekly] 维度分桶拼接失败: {type(de).__name__}: {str(de)[:80]}')

        # ⭐ 追加"推荐但未持仓"明细(具体票名 + 真实收益), 让用户看错过的机会 / 幸亏没买的雷
        try:
            from portfolio_db import portfolio_db
            held = {str(s.get('code') or '').zfill(6)
                    for s in (portfolio_db.get_all_stocks() or [])
                    if s.get('code')}
            unowned_section = format_unowned_picks(held, days=30, top_winners=10, top_losers=5)
            if unowned_section:
                report_text += '\n' + unowned_section + '\n'
        except Exception as ue:
            print(f'[ai_eval_weekly] 未持仓明细拼接失败: {type(ue).__name__}: {str(ue)[:80]}')

        try:
            from notification_router import send
            send('archive', 'AI 推荐周报', report_text)
        except Exception as ne:
            print(f'[ai_eval_weekly] 推送失败: {ne}\n{report_text}')

        _log_run(job, 'success',
                 error=f'samples={overall.sample_size} score={overall.score}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_eod_outcomes():
    """🎯 盘后后验合并(16:55)—— 合 ai_rec_check + decision_signal_outcomes 为一个任务:
    收盘 K线焐热后,①推荐池收盘价回填胜率(check_all_active,喂 ai_eval_weekly)
    ②决策信号过 horizon 判 hit/miss(run_outcomes)。二者都是"盘后读K线/行情做后验",合一个少一环
    盘后链。两段各自 try 包裹:一段失败不拖另一段。开关 eod_outcomes(默认开)。非交易日跳过。"""
    job = 'eod_outcomes'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):   # 非交易日不跑(K线无新 bar)
        return
    _wait_kline_prefetch(job)       # F: 等 kline_prefetch 焐完缓存再后验(读暖缓存,不冷拉)
    started = datetime.now().isoformat()
    parts = []
    # ① 推荐池收盘价回填(原 ai_rec_check)
    try:
        from ai_recommendation_monitor import check_all_active
        s = check_all_active()
        parts.append(f"rec: checked={s['checked']} tp={s['hit_target']} sl={s['hit_stop']}")
    except Exception as e:
        parts.append(f"rec_err={type(e).__name__}:{str(e)[:50]}")
    # ② 决策信号后验(原 decision_signal_outcomes)
    try:
        from decision_signal import run_outcomes
        r = run_outcomes(days=35)   # 35天足覆盖最长 horizon(long=20交易日)+缓冲
        parts.append(f"signals: eval={r.get('evaluated')} hit={r.get('hit')} miss={r.get('miss')}")
    except Exception as e:
        parts.append(f"signal_err={type(e).__name__}:{str(e)[:50]}")
    _log_run(job, 'success', error=' | '.join(parts),
             started_at=started, finished_at=datetime.now().isoformat())


def _fund_gate(job: str) -> bool:
    """基金任务统一开关 + 交易日守卫。返回 True 表示应跳过。"""
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return True
    except Exception:
        pass
    return _skip_if_not_trading(job)


def task_fund_nav_refresh():
    """🏦 盘后拉基金净值 + 计算基金单日总收益存表(开关 fund_nav_refresh,需手动开启)。"""
    job = 'fund_nav_refresh'
    if _fund_gate(job):
        return
    started = datetime.now().isoformat()
    try:
        import fund_db, fund_data
        fund_db.init_db()
        codes = {h['code'] for h in fund_db.get_holdings()}
        codes |= {p['code'] for p in fund_db.get_plans(only_enabled=True)}
        saved = 0
        nav_cache = {}  # {code: {'unit_nav': float, 'daily_return': float or None}}
        for code in sorted(codes):
            df = fund_data.get_nav_history(code)
            if df is not None and not df.empty:
                saved += fund_db.save_nav(code, df.tail(120))
                last = df.iloc[-1]
                try:
                    unav = float(last['unit_nav'])
                except (TypeError, ValueError, KeyError):
                    unav = None
                try:
                    dr = float(last.get('daily_return'))
                except (TypeError, ValueError):
                    dr = None
                nav_cache[code] = {'unit_nav': unav, 'daily_return': dr, 'nav_date': str(last['date'])[:10]}
        # 落组合净值快照
        snap = fund_db.save_portfolio_snapshot(
            datetime.now().strftime('%Y-%m-%d'),
            nav_lookup=lambda c: (nav_cache.get(c) or {}).get('unit_nav'))
        # ─── 计算基金单日总收益并写入 daily_pnl_snapshots ───
        # 只纳入当日净值已出的基金，避免混入不同日期的日增长率
        snap_date = datetime.now().strftime('%Y-%m-%d')
        holdings = fund_db.get_holdings()
        fund_count = fund_mv = fund_daily_pnl = 0
        for h in holdings:
            code = h['code']
            shares = h.get('shares', 0) or 0
            if shares <= 0:
                continue
            info = nav_cache.get(code, {})
            unit_nav = info.get('unit_nav')
            nav_date = info.get('nav_date', '')
            if unit_nav is None:
                continue
            mv = shares * unit_nav
            fund_mv += mv
            fund_count += 1
            # 只算当日净值对应收益，跨日不混
            if nav_date == snap_date:
                daily_r = info.get('daily_return')
                if daily_r is not None:
                    fund_daily_pnl += mv * daily_r / 100
        fund_daily_pct = (fund_daily_pnl / (fund_mv - fund_daily_pnl) * 100) if (fund_mv - fund_daily_pnl) > 0 else 0
        try:
            from portfolio.daily_pnl import upsert_fund_pnl
            upsert_fund_pnl(snap_date, fund_count, round(fund_mv, 2),
                           round(fund_daily_pnl, 2), round(fund_daily_pct, 4))
        except Exception:
            pass
        _log_run(job, 'success',
                 error=f'funds={len(codes)} nav_rows={saved} snap={snap} '
                       f'fund_pnl={fund_daily_pnl:.0f}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_fund_dca_reminder():
    """🏦 到期定投处理:开了 auto_record 的计划**自动记账**(按最新确认净值,幂等防重);
    其余只提醒,需手动申购+记账(开关 fund_dca_reminder,默认开)。"""
    job = 'fund_dca_reminder'
    if _fund_gate(job):
        return
    started = datetime.now().isoformat()
    try:
        import fund_db, fund_dca
        fund_db.init_db()
        today = datetime.now()
        # 1) 自动记账(开了 auto_record 的到期计划)
        recorded = fund_dca.auto_record_due_plans(today)
        recorded_codes = {r['code'] for r in recorded}
        # 2) 仅提醒(到期但未开自动记账的)
        remind = [p for p in fund_db.get_plans(only_enabled=True)
                  if fund_dca.is_due(p, today) and not p.get('auto_record')]
        parts = []
        if recorded:
            parts.append("✅ 已自动记账:\n" + "\n".join(
                f"· {r['code']} 定投 {r['amount']:.0f} 元 → 持仓 {r['pos_shares']:.2f} 份" for r in recorded))
        if remind:
            parts.append("🔔 待手动定投:\n" + "\n".join(
                f"· {p['code']} {p.get('name') or ''} 定投 {p['amount']:.0f} 元({p['strategy']})" for p in remind))
        if parts:
            text = "🏦 今日定投\n" + "\n\n".join(parts)
            try:
                from notification_router import send
                send('report', '基金定投', text)
            except Exception as ne:
                print(f'[fund_dca_reminder] 推送失败: {ne}\n{text}')
        _log_run(job, 'success', error=f'auto={len(recorded)} remind={len(remind)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_daily_pnl_snapshot():
    """💰 22:30 盘后合并：读股票快照的 daily_mv_change + 基金日收益 → 落 daily_pnl_snapshots"""
    job = 'daily_pnl_snapshot'
    if _skip_if_not_trading(job):   # 非交易日无当日快照:跳过,免空跑(原靠 merge_save 返 None 兜底)
        return
    started = datetime.now().isoformat()
    snap_date = datetime.now().strftime('%Y-%m-%d')
    try:
        from portfolio.daily_pnl import merge_save, get_summary
        r = merge_save(snap_date)
        status = 'success' if r else 'skipped'
        err = f'stock={r["stock_count"]}/{r["stock_daily_pnl"]:.0f} fund={r["fund_count"]}/{r["fund_daily_pnl"]:.0f}' if r else 'no data'
        # 收盘后推送"今日盈亏"一条(通知驱动:一眼看到今天股票+基金合计赚亏)
        if r:
            try:
                tp = r.get('total_daily_pnl', 0) or 0
                tpct = r.get('total_daily_pct', 0) or 0
                # A 股惯例:红涨绿跌(盈利红, 亏损绿)
                emoji = '🔴' if tp > 0 else ('🟢' if tp < 0 else '⚪')
                s = get_summary(60) or {}
                _fund_note = ' ⚠️基金数据缺失未计入' if r.get('fund_pending') else ''
                lines = [
                    f"{emoji} 今日盈亏 {tp:+,.0f} 元 ({tpct:+.2f}%){_fund_note}",
                    f"  股票 {r.get('stock_daily_pnl', 0):+,.0f}({r.get('stock_count', 0)}只) · 基金 {r.get('fund_daily_pnl', 0):+,.0f}({r.get('fund_count', 0)}只)",
                ]
                if s.get('mtd_pnl') is not None:
                    lines.append(f"  本月累计 {s['mtd_pnl']:+,.0f} · 近{s.get('period_days', 0)}日 {s.get('period_pnl', 0):+,.0f}(胜率{s.get('win_rate', 0)}%)")
                from notification_router import send
                send('report', f"💰 今日盈亏 {tp:+,.0f}", '\n'.join(lines))
            except Exception as pe:
                print(f'[daily_pnl_snapshot] 推送失败: {pe}')
        _log_run(job, status, error=err, started_at=started,
                 finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_fund_target_check():
    """🏦 对设了止盈目标的定投计划,按最新净值算持有浮盈,达标则提醒赎回(开关 fund_target_check,默认开)。"""
    job = 'fund_target_check'
    if _fund_gate(job):
        return
    started = datetime.now().isoformat()
    try:
        import fund_db, fund_data
        fund_db.init_db()
        holdings = {h['code']: h for h in fund_db.get_holdings()}
        hits = []
        for p in fund_db.get_plans(only_enabled=True):
            target = p.get('target_profit_pct')
            h = holdings.get(p['code'])
            if not target or not h or not h.get('cost_nav'):
                continue
            latest = fund_data.latest_nav(p['code'])
            if not latest or not latest.get('unit_nav'):
                continue
            pnl = (latest['unit_nav'] - h['cost_nav']) / h['cost_nav']
            if pnl >= float(target):
                hits.append(f"· {p['code']} {p.get('name') or ''} 浮盈 {pnl:+.2%} ≥ 目标 {float(target):+.0%}")
        if hits:
            text = "🏦 定投止盈提醒(达标)\n" + "\n".join(hits) + "\n建议结合估值与计划评估是否止盈赎回。"
            try:
                from notification_router import send
                send('report', '基金止盈提醒', text)
            except Exception as ne:
                print(f'[fund_target_check] 推送失败: {ne}\n{text}')
        _log_run(job, 'success', error=f'hits={len(hits)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_fund_evening():
    """🏦 基金晚间合并(22:00)—— 合 fund_nav_refresh + fund_target_check 为一个调度入口:
    顺序跑 ①盘后拉净值入库+算基金单日收益 → ②按最新净值查定投止盈(止盈依赖新净值,故必须先刷后查)。
    两子任务各自的开关(fund_nav_refresh/fund_target_check)+日志仍独立有效、可单独关;本任务只是统一
    顺序调度。开关 fund_evening(默认开;关掉则两步都不跑)。两段各自 try:一段异常不拖另一段。"""
    job = 'fund_evening'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return
    except Exception:
        pass
    try:
        task_fund_nav_refresh()      # ① 先刷净值(内含 _fund_gate + _log_run + 异常兜底)
    except Exception as e:
        print(f'[fund_evening] 净值入库异常(继续查止盈): {type(e).__name__}: {str(e)[:80]}')
    try:
        task_fund_target_check()     # ② 再查止盈(读 ① 刷出的新净值)
    except Exception as e:
        print(f'[fund_evening] 止盈检查异常: {type(e).__name__}: {str(e)[:80]}')


def task_fund_valuation_signal():
    """🏦 宽基指数估值分位播报:扫常用指数,低估(分位<50%,倍数≥1.5)的提示加投(开关 fund_valuation_signal,默认开)。"""
    job = 'fund_valuation_signal'
    if _fund_gate(job):
        return
    started = datetime.now().isoformat()
    try:
        import fund_valuation
        rows = []
        # 加进度日志: 每个指数耗时 / 成败, 卡死时一眼能看出卡在哪只
        for idx in fund_valuation.COMMON_INDEXES:
            _t0 = time.time()
            try:
                v = fund_valuation.index_pe_percentile(idx)
                if v:
                    rows.append(v)
                    print(f'[fund_valuation] ✅ {idx} 耗时 {time.time()-_t0:.1f}s', flush=True)
                else:
                    print(f'[fund_valuation] ⚠️ {idx} 返回空 ({time.time()-_t0:.1f}s)', flush=True)
            except Exception as e:
                print(f'[fund_valuation] ❌ {idx} {type(e).__name__}: {str(e)[:80]} ({time.time()-_t0:.1f}s)', flush=True)
        cheap = [v for v in rows if v['multiplier'] >= 1.5]
        if rows:
            lines = [f"· {v['index']}: PE{v['pe']} 分位{v['percentile']:.0f}% [{v['level']}] {v['multiplier']}x"
                     for v in sorted(rows, key=lambda x: x['percentile'])]
            text = "🏦 宽基估值分位(定投择时)\n" + "\n".join(lines)
            if cheap:
                text += "\n\n💡 偏低估、可加投:" + "、".join(v['index'] for v in cheap)
            try:
                from notification_router import send
                send('report', '基金估值分位', text)
            except Exception as ne:
                print(f'[fund_valuation_signal] 推送失败: {ne}\n{text}')
        _log_run(job, 'success', error=f'indexes={len(rows)} cheap={len(cheap)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_rag_ingest():
    """🔎 每日把历史分析/新闻/推荐 嵌入入 pgvector(语义检索语料保鲜)。开关 rag_ingest,默认开。"""
    job = 'rag_ingest'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return
    except Exception:
        pass
    started = datetime.now().isoformat()
    try:
        import sys, os as _os
        sys.path.insert(0, _os.path.join(_bootstrap.ROOT, 'rag'))
        import service
        r = service.ingest_all(news_limit=1000)
        _log_run(job, 'success', error=str(r),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_pg_backup():
    """💾 每日把生产 PG 全量备份到本地 SQLite(db/pg_backup.db)。开关 pg_backup,默认开。"""
    job = 'pg_backup'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return
    except Exception:
        pass
    started = datetime.now().isoformat()
    try:
        from cache import lock
        with lock('job:pg_backup', ttl=1800) as ok:   # 防多实例并发备份
            if not ok:
                _log_run(job, 'skipped', error='another backup running (lock held)',
                         started_at=started, finished_at=datetime.now().isoformat())
                return
            from pg_backup import backup_pg_to_sqlite
            r = backup_pg_to_sqlite()
        _log_run(job, 'success', error=f"tables={r['tables']} rows={r['rows']} -> {r['file']}",
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_daily_market_snapshot():
    """采集大盘+北向资金快照（盘后跑）"""
    job = 'daily_market_snapshot'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        payload = {}
        try:
            payload['north_flow'] = datahub.north_flow(30)
        except Exception as e:
            payload['north_flow_error'] = str(e)
        try:
            payload['dragon_tiger'] = datahub.dragon_tiger()
        except Exception as e:
            payload['dragon_tiger_error'] = str(e)
        save_market_snapshot(payload)
        _log_run(job, 'success', error=None, started_at=started,
                 finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


# =============================================================================
# Jobs Hub 单例
# =============================================================================

# 任务失败通知限频:同一任务的错误通知至少间隔此秒数,避免 30min 级任务每轮刷屏(进程内,重启重置)
_ERR_NOTIFY_LAST: Dict[str, float] = {}
_ERR_NOTIFY_COOLDOWN = 7200  # 2h


def _notify_task_error(name: str, exc: Exception, tb: str):
    """任务失败 → 推一条告警(默认 QQ),便于第一时间发现+后续修复。同任务 2h 内限一条。"""
    try:
        now = time.time()
        if now - _ERR_NOTIFY_LAST.get(name, 0) < _ERR_NOTIFY_COOLDOWN:
            return
        _ERR_NOTIFY_LAST[name] = now
        cn = name
        try:
            from automation_config import REGISTRY
            cn = REGISTRY.get(name, {}).get('cn', name)
        except Exception:
            pass
        body = (f"任务「{cn}」({name}) 执行失败\n"
                f"{type(exc).__name__}: {str(exc)[:300]}\n\n"
                f"——traceback 尾——\n{tb[-600:]}")
        from notification_router import send
        send('alert', f"⚠️ 任务失败: {name}", body)
    except Exception:
        pass  # 通知失败绝不影响主流程


def _notify_data_unavailable(name: str, detail: str = ''):
    """全靠外部数据的任务因数据源不可用/超时而结束 → 推一条平和提示(非告警, 不带 traceback)。
    按用户要求:"外部接口失败了就提示, 然后任务结束"。同任务 2h 内限一条(复用告警限频表)。"""
    try:
        now = time.time()
        if now - _ERR_NOTIFY_LAST.get(name, 0) < _ERR_NOTIFY_COOLDOWN:
            return
        _ERR_NOTIFY_LAST[name] = now
        cn = name
        try:
            from automation_config import REGISTRY
            cn = REGISTRY.get(name, {}).get('cn', name)
        except Exception:
            pass
        body = f"任务「{cn}」({name}) 因外部数据源暂不可用而结束本次{('(' + detail + ')') if detail else ''}, 已自动跳过, 下个周期重试。"
        from notification_router import send
        send('report', f"📡 数据源暂不可用: {cn}", body)
    except Exception:
        pass


# ─── 任务运行时长追踪 + deadman switch (2026-06-15, 06-17 调方案) ─────────────
# 背景:6/14 夜里 pywencai 卡死, 整个线程池被 6 个僵尸任务堵满, 调度器主线程还活着
#       但 supervisor 看 PID 在不重启 → 09:45 选股没跑。
# 方案(06-17 改):
#   软超时 SOFT_DEADLINE_SEC(默认 30min): 只打 ⚠️ + 记 job_runs, 不杀进程。
#     原始方案 30 分钟就 hard exit 整个 jobs_hub, 连带杀其它正在跑的任务 → 选股/回测
#     这种本身就慢的任务(InStock 取因子 + 大量 K 线)会反复被误杀, 永远出不来结果。
#   硬超时 HARD_DEADLINE_SEC(默认 90min): 任意单任务超这个时间 → os._exit, 真正失控才杀。
#   僵尸检测:thread pool 全部 6 slot 都被超软超时的任务占据 → 也 os._exit(真僵尸)。
_TASK_START_TS: Dict[str, float] = {}                     # task name -> 开始时间戳
_TASK_ALERTED: 'set[tuple[str, float]]' = set()           # 已告警过的 (name, ts), 防刷屏
_TASK_LOCK = threading.Lock()
import os as _os
SOFT_DEADLINE_SEC = int(_os.environ.get('JOBS_HUB_TASK_DEADLINE_SEC', '1800'))   # 默认 30min: 软告警
HARD_DEADLINE_SEC = int(_os.environ.get('JOBS_HUB_TASK_HARD_DEADLINE_SEC',
                                        str(SOFT_DEADLINE_SEC * 3)))             # 默认 90min: 硬杀
# 兼容旧变量名(配置 / 文档引用过 TASK_DEADLINE_SEC, 不破坏)
TASK_DEADLINE_SEC = SOFT_DEADLINE_SEC

# ─── 全局任务硬超时(2026-06-23): 任何任务超过阈值都被外层 future 切断 ────────
# 背景:不少任务(fund_valuation_signal / morning_portfolio / selection_debate /
# InStock 取因子 等)就算我加了内层 timeout, 实际还是被某些底层 socket / lock
# 卡 30+ 分钟, 拖累后续任务窗口。最稳:在 _run_with_log 调用 func 时套外层 future,
# 任意任务超 _TASK_HARD_TIMEOUTS[name] 必抛 TimeoutError(孤儿线程留底层自然结束)。
#
# 配置默认每任务 10 分钟(_DEFAULT_TASK_TIMEOUT), 慢任务用 _TASK_HARD_TIMEOUTS 单独
# 调高(rag_ingest / daily_backtest 等)。可通过 env JOBS_HUB_DEFAULT_TASK_TIMEOUT 覆盖。
_DEFAULT_TASK_TIMEOUT = int(_os.environ.get('JOBS_HUB_DEFAULT_TASK_TIMEOUT', '600'))   # 10 分钟
_TASK_HARD_TIMEOUTS: Dict[str, int] = {
    # 慢任务的专属超时(秒)
    'rag_ingest':                3600,   # 嵌入入库 ~13 分钟历史值, 给 1 小时
    'daily_backtest':            3600,   # 历史回测
    'factor_collection':         1200,
    'kline_prefetch':            1800,   # 焐 raw+qfq 两套 K线 + full_valuation(2026-06-25),给足时间
    'mx_daily_analysis':         1500,   # LLM 慢
    'mx_selection_review':       1500,
    'sector_rotation':           900,    # 📈 题材轮动雷达:智策多 agent LLM 分析,给 15 分钟
    'overnight_strategy':        2400,   # 隔夜大批 AI 分析
    'announcement_scan':         1500,   # 三合一(解禁+公告+研报,2026-06-24),含多次 LLM
    'main_force_prefetch':       180,    # 盘前预取主力选股:问财90s + akshare兜底,给3分钟
    'unified_selection':         1800,   # 综合选股(5大策略+InStock+多因子)+ 红蓝对抗整合(10只LLM)
    'morning_portfolio':         900,
    'afternoon_portfolio':       900,
    'portfolio_indicator_snapshot': 1200,
    'selection_debate':          900,
    'fund_valuation_signal':     300,    # 6 个指数, 5 分钟够
    'fund_dca_reminder':         300,
    'fund_target_check':         600,
    'fund_nav_refresh':          900,
    'fund_evening':              1200,   # B 合并:净值入库(900) + 止盈检查,串行给足
    'eod_outcomes':              900,    # A 合并:等 prefetch(≤300) + 推荐池回填 + 信号后验
    'pg_backup':                 600,
}
_TASK_TIMEOUT_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _run_with_log(name, func, *a, **kw):
    """Run task in thread pool and log result (module-level, usable by _wrap)。

    1. 进 / 出 / 失败时都在 stdout 打一行带耗时的标识, 方便 grep 出某任务的执行窗口。
    2. 套全局外层 future 硬超时(_DEFAULT_TASK_TIMEOUT, 慢任务在 _TASK_HARD_TIMEOUTS 覆盖):
       任何任务超时一律切断, 不再卡 30 分钟拖累后续。孤儿线程留底层自然结束。
    3. 异常 → 记 job_runs(带 traceback 尾) + 推 alert 告警(限频)。
    """
    global _TASK_TIMEOUT_POOL
    if _TASK_TIMEOUT_POOL is None:
        _TASK_TIMEOUT_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=24, thread_name_prefix='task-hard-timeout')

    t0 = time.time()
    pool_busy = len(_TASK_START_TS) + 1
    timeout = _TASK_HARD_TIMEOUTS.get(name, _DEFAULT_TASK_TIMEOUT)
    print(f'[jobs_hub] ▶ {name} 开始 (pool={pool_busy}/6, 超时{timeout}s)', flush=True)
    with _TASK_LOCK:
        _TASK_START_TS[name] = t0
    fut = _TASK_TIMEOUT_POOL.submit(func, *a, **kw)
    try:
        fut.result(timeout=timeout)
        elapsed = time.time() - t0
        print(f'[jobs_hub] ✅ {name} 完成 (耗时 {elapsed:.1f}s)', flush=True)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        elapsed = time.time() - t0
        msg = f'task 超过硬超时 {timeout}s 被切断(孤儿线程留底层)'
        print(f'[jobs_hub] ⏱️ {name} 超时切断 (耗时 {elapsed:.1f}s, 阈值 {timeout}s)', flush=True)
        _log_run(name, 'error', error=msg, notify=False)
        # 超时基本都是"外部数据源卡死/不可用"导致(datahub 源级已 20s 兜底, 任务级超时
        # 通常意味数据源整体抽风)。按用户要求: 发一条平和"数据源不可用"提示, 任务正常结束,
        # 不发吓人的"⚠️任务失败+traceback"告警(避免误以为代码崩了)。限频同 _notify_task_error。
        _notify_data_unavailable(name, f'运行 {elapsed:.0f}s 超时(阈值 {timeout}s)')
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        elapsed = time.time() - t0
        print(f'[jobs_hub] ❌ {name} 失败 (耗时 {elapsed:.1f}s): '
              f'{type(e).__name__}: {str(e)[:120]}', flush=True)
        _log_run(name, 'error', error=f'{type(e).__name__}: {e}\n{tb[-900:]}', notify=False)
        _notify_task_error(name, e, tb)
    finally:
        with _TASK_LOCK:
            _TASK_START_TS.pop(name, None)


class _JobsHub:
    def __init__(self):
        self._registered: List[Dict] = []
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # ⭐ 线程池：所有任务异步执行，不阻塞调度 loop
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)

    def register(self, name: str, when: str, func: Callable, *args, **kwargs):
        """注册定时任务

        when: 'HH:MM' 每日时间，或 'every:N:minutes' 间隔
        """
        if when.startswith('every:'):
            _, n, unit = when.split(':')
            unit_method = getattr(schedule.every(int(n)), unit, None)
            if unit_method is None:
                raise ValueError(f'unsupported unit: {unit}')
            job = unit_method.do(self._wrap(name, func), *args, **kwargs)
        else:
            # TZ=Asia/Shanghai 已设，schedule 内部用 datetime.now() 即 CST，直接传
            job = schedule.every().day.at(when).do(self._wrap(name, func), *args, **kwargs)
        self._registered.append({'name': name, 'when': when, 'job': job})

    def _wrap(self, name: str, func: Callable):
        """包装任务函数为异步执行，不阻塞调度线程。
        2026-06-12:在唯一调度入口加开关闸门 —— 凡在 automation_config.REGISTRY 里登记的任务,
        开关关闭则静默跳过(不进线程池、不刷日志,避免 30min 级任务刷屏);未登记的任务照常运行;
        开关系统异常 → 照常运行(绝不因配置层故障误杀定时任务)。这样 webui 的开关对核心任务真正生效。"""
        def runner(*a, **kw):
            try:
                from automation_config import is_enabled, REGISTRY
                if name in REGISTRY and not is_enabled(name):
                    return
            except Exception:
                pass
            try:
                self._executor.submit(_run_with_log, name, func, *a, **kw)
            except Exception as e:
                _log_run(name, 'error', error=f'提交线程池失败: {e}')
        return runner


    def list_jobs(self) -> List[Dict]:
        return [{'name': j['name'], 'when': j['when'], 'next_run': str(j['job'].next_run)}
                for j in self._registered]

    def list_recent_runs(self, limit: int = 50) -> List[Dict]:
        conn = db_connect(_SNAPSHOT_DB_PATH)
        cur = conn.cursor()
        cur.execute('''
            SELECT job_name, started_at, finished_at, status, error
            FROM job_runs ORDER BY id DESC LIMIT ?
        ''', (limit,))
        rows = cur.fetchall()
        conn.close()
        return [
            {'job_name': r[0], 'started_at': r[1], 'finished_at': r[2],
             'status': r[3], 'error': r[4]} for r in rows
        ]

    def start(self):
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                try:
                    schedule.run_pending()
                except Exception as e:
                    print(f'[jobs_hub] 调度线程异常: {e}')
                    import traceback
                    traceback.print_exc()
                time.sleep(1)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

        # ⭐ deadman switch(06-17 改方案):
        #   - 软超时 > SOFT_DEADLINE_SEC: 每个 (name, ts) 只打一次 ⚠️ + 记 job_runs, 不杀进程
        #     (任务可能本身就慢, 如 InStock 取因子, 让它继续跑完)
        #   - 硬超时 > HARD_DEADLINE_SEC 任一任务: 真失控, 整体退出由 supervisor 拉起
        #   - 僵尸: 线程池 6 个 slot 全部都超软超时 → 进程死锁, 也整体退出
        def _deadman():
            pool_size = self._executor._max_workers  # 6
            while self._running:
                try:
                    now = time.time()
                    new_alerts = []      # 本轮新增的软告警(去重)
                    over_soft = []       # 当前所有超软超时的任务
                    over_hard = []       # 当前所有超硬超时的任务
                    with _TASK_LOCK:
                        for n, ts in list(_TASK_START_TS.items()):
                            age = now - ts
                            if age > SOFT_DEADLINE_SEC:
                                over_soft.append((n, ts, int(age)))
                                if (n, ts) not in _TASK_ALERTED:
                                    new_alerts.append((n, int(age)))
                                    _TASK_ALERTED.add((n, ts))
                            if age > HARD_DEADLINE_SEC:
                                over_hard.append((n, int(age)))
                        # 清理已结束任务的告警记录, 防内存泄漏
                        live = {(n, ts) for n, ts in _TASK_START_TS.items()}
                        _TASK_ALERTED.intersection_update(live)

                    # 软告警: 每任务只打一次 ⚠️, 不杀
                    if new_alerts:
                        msg = ', '.join(f'{n}({d}s)' for n, d in new_alerts)
                        print(f'[jobs_hub] ⚠️ 任务超时但继续等 (>{SOFT_DEADLINE_SEC}s): {msg}',
                              flush=True)
                        try:
                            for n, d in new_alerts:
                                _log_run(n, 'error', error=f'soft_timeout {d}s, 继续运行')
                        except Exception:
                            pass

                    # 硬超时: 任一任务 > HARD_DEADLINE_SEC, 整体退出
                    if over_hard:
                        msg = ', '.join(f'{n}({d}s)' for n, d in over_hard)
                        print(f'[jobs_hub] 💀 任务硬超时 (>{HARD_DEADLINE_SEC}s): {msg} — '
                              f'os._exit(99), supervisor 重启', flush=True)
                        try:
                            for n, d in over_hard:
                                _log_run(n, 'error', error=f'hard_timeout {d}s')
                        except Exception:
                            pass
                        _os._exit(99)

                    # 僵尸检测: 线程池满 + 所有 slot 都超软超时 → 真死锁
                    if len(over_soft) >= pool_size:
                        msg = ', '.join(f'{n}({d}s)' for n, ts, d in over_soft)
                        print(f'[jobs_hub] 💀 线程池({pool_size}) 全部任务都超时, 死锁判定 — '
                              f'os._exit(99), supervisor 重启: {msg}', flush=True)
                        try:
                            for n, ts, d in over_soft:
                                _log_run(n, 'error', error=f'pool_deadlock {d}s')
                        except Exception:
                            pass
                        _os._exit(99)
                except Exception as e:
                    print(f'[jobs_hub] deadman 线程异常: {e}', flush=True)
                time.sleep(30)
        threading.Thread(target=_deadman, name='jobs_hub-deadman', daemon=True).start()

    def stop(self):
        self._running = False


hub = _JobsHub()


# ── 妙想(可选独立模块) ──
def mx_second_opinion():
    """妙想第二意见 — 独立可选,异常静默不影响主流程"""
    try:
        from jobs.mx_advisor import mx_second_opinion as _mx
        _mx()
    except Exception as e:
        print(f'[jobs_hub] mx_second_opinion 跳过(独立模块不可用): {e}')

def mx_daily_wrap():
    """妙想每日综述 — 独立可选,异常静默不影响主流程"""
    try:
        from jobs.mx_advisor import mx_daily_wrap as _mx
        _mx()
    except Exception as e:
        print(f'[jobs_hub] mx_daily_wrap 跳过(独立模块不可用): {e}')


# =============================================================================
# 新增预置任务 — 盘前预热 / 选股扫描 / 龙虎榜归档 / 周清理
# =============================================================================

def task_dragon_tiger_archive():
    """龙虎榜归档：每个交易日盘后拉取并存库（不做 AI 分析）"""
    job = 'dragon_tiger_archive'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        from longhubang_data import LonghubangDataFetcher
        from longhubang_db import get_longhubang_db
        today = datetime.now().strftime('%Y-%m-%d')
        fetcher = LonghubangDataFetcher()
        data = fetcher.get_longhubang_data(today) or []
        if not data:
            _log_run(job, 'skipped', error=f'no data for {today}',
                     started_at=started, finished_at=datetime.now().isoformat())
            return
        db = get_longhubang_db()
        saved = db.save_longhubang_data(data) if hasattr(db, 'save_longhubang_data') else 0
        _log_run(job, 'success', error=f'fetched={len(data)} saved={saved}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


_STRATEGY_TIMEOUT_POOL: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _call_with_hard_timeout(label: str, fn: Callable, timeout: int = 120):
    """套硬超时调 fn() — 不论内部 pywencai/akshare/socket 如何卡死, timeout 秒后必抛 TimeoutError。
    背景: pywencai_safe 内部已 90s 超时, 但实测 unified_selection 仍出现单策略卡 30 分钟,
    可能是底层 socket/lock/线程池死锁绕开了内层超时。在调用层加外层 future timeout 兜底,
    任何"莫名卡"都被切断 → 5 大策略中坏掉一两个也不拖死整个选股流程。"""
    global _STRATEGY_TIMEOUT_POOL
    if _STRATEGY_TIMEOUT_POOL is None:
        _STRATEGY_TIMEOUT_POOL = concurrent.futures.ThreadPoolExecutor(
            max_workers=5, thread_name_prefix='strategy-timeout')
    fut = _STRATEGY_TIMEOUT_POOL.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        fut.cancel()
        raise TimeoutError(f'{label} 超时 {timeout}s (孤儿线程留给底层自然结束)')


def _run_strategy_scans() -> dict:
    """执行5大策略扫描，返回汇总结果。单个策略失败不影响其他，并推送告警。
    每个策略调用都套 120s 硬超时(外层兜底, 防底层卡死绕过内部超时)。"""
    results = {}
    try:
        from low_price_bull_selector import LowPriceBullSelector
        ok, df, msg = _call_with_hard_timeout(
            '低价擒牛',
            lambda: LowPriceBullSelector().get_low_price_stocks(top_n=5),
            timeout=120)
        results['低价擒牛'] = (ok, df, msg)
        if not ok:
            _push_error('策略扫描失败-低价擒牛', msg)
    except Exception as e:
        results['低价擒牛'] = (False, None, str(e))
        _push_error('策略扫描失败-低价擒牛', str(e))
    try:
        from small_cap_selector import SmallCapSelector
        ok, df, msg = _call_with_hard_timeout(
            '小市值',
            lambda: SmallCapSelector().get_small_cap_stocks(top_n=5),
            timeout=120)
        results['小市值'] = (ok, df, msg)
        if not ok:
            _push_error('策略扫描失败-小市值', msg)
    except Exception as e:
        results['小市值'] = (False, None, str(e))
        _push_error('策略扫描失败-小市值', str(e))
    try:
        from profit_growth_selector import ProfitGrowthSelector
        ok, df, msg = _call_with_hard_timeout(
            '净利增长',
            lambda: ProfitGrowthSelector().get_profit_growth_stocks(top_n=5),
            timeout=120)
        results['净利增长'] = (ok, df, msg)
        if not ok:
            _push_error('策略扫描失败-净利增长', msg)
    except Exception as e:
        results['净利增长'] = (False, None, str(e))
        _push_error('策略扫描失败-净利增长', str(e))
    try:
        from value_stock_selector import ValueStockSelector
        ok, df, msg = _call_with_hard_timeout(
            '低估值',
            lambda: ValueStockSelector().get_value_stocks(top_n=5),
            timeout=120)
        results['低估值'] = (ok, df, msg)
        if not ok:
            _push_error('策略扫描失败-低估值', msg)
    except Exception as e:
        results['低估值'] = (False, None, str(e))
        _push_error('策略扫描失败-低估值', str(e))
    try:
        from main_force_selector import MainForceStockSelector
        def _do_main_force():
            mf = MainForceStockSelector()
            # 读盘前 09:15 main_force_prefetch 写的当日缓存;冷了才现调问财(选股高峰不卡问财)
            r_ok, r_df, r_msg = mf.get_main_force_stocks_cached(days_ago=5, use_cache=True)
            if r_ok and r_df is not None and len(r_df) > 0:
                r_df = mf.get_top_stocks(r_df, top_n=5)
            return r_ok, r_df, r_msg
        ok, df, msg = _call_with_hard_timeout('主力资金', _do_main_force, timeout=120)
        results['主力资金'] = (ok, df, msg)
        if not ok:
            _push_error('策略扫描失败-主力资金', msg)
    except Exception as e:
        results['主力资金'] = (False, None, str(e))
        _push_error('策略扫描失败-主力资金', str(e))

    total = sum(len(df) for _, df, _ in results.values() if df is not None and len(df) > 0)
    return {'results': results, 'total': total}


def _format_strategy_results(results: dict) -> str:
    """把策略扫描结果拼成Markdown表格，包含实时行情"""
    # 收集所有选中股票代码
    all_codes = []
    code_to_strategies = {}  # code -> [策略名列表]
    for strategy, (ok, df, msg) in results.items():
        if ok and df is not None and len(df) > 0:
            for _, row in df.iterrows():
                code = next((row[c] for c in ['股票代码', 'code'] if c in row.index), '')
                if code and len(all_codes) < 20:  # 最多20只
                    if code not in code_to_strategies:
                        all_codes.append(code)
                    code_to_strategies.setdefault(code, []).append(strategy)

    if not all_codes:
        return '━━ 📊 盘前策略扫描 ━━━\n  所有策略均无候选'

    # 一次批量获取实时行情
    quotes = {}
    try:
        raw = datahub.quotes([c for c in all_codes])
        if raw:
            quotes = raw
    except Exception:
        pass

    lines = ['━━ 📊 盘前策略扫描 ━━━']
    # Markdown 表格头
    lines.append('| # | 策略 | 代码 | 名称 | 价格 | PE | PB | 涨跌% | 换手% | 市值 |')
    lines.append('|---|------|------|------|------|-----|-----|-------|-------|------|')

    idx = 0
    for strategy, (ok, df, msg) in results.items():
        if not ok or df is None or len(df) == 0:
            continue
        for _, row in df.iterrows():
            code = next((row[c] for c in ['股票代码', 'code'] if c in row.index), '')
            if not code or idx >= 15:
                continue
            # dataapi基础数据
            name = next((row[c] for c in ['股票简称', 'name'] if c in row.index), '')
            price = next((row[c] for c in ['股价', 'price'] if c in row.index), '')
            pe = next((row[c] for c in ['pe', 'PE'] if c in row.index), '')
            pb = next((row[c] for c in ['pb', 'PB'] if c in row.index), '')

            # 实时行情补充
            q = (quotes.get(code) or quotes.get(str(code)[-6:]) or {})
            change = q.get('change_pct', '')
            turnover = q.get('turnover_pct', '')
            mcap = q.get('mcap_yi', '')
            price = q.get('price', price)  # 用实时价覆盖
            pe = q.get('pe_ttm', pe)
            pb = q.get('pb', pb)

            price_s = f'{float(price):.2f}' if price else '-'
            pe_s = f'{float(pe):.1f}' if pe and float(pe) > 0 else '-'
            pb_s = f'{float(pb):.2f}' if pb and float(pb) > 0 else '-'
            change_s = f'{float(change):+.2f}%' if change else '-'
            turnover_s = f'{float(turnover):.2f}%' if turnover else '-'
            mcap_s = f'{float(mcap):.0f}亿' if mcap else '-'
            idx += 1
            # 策略标签缩写
            tag = strategy[:2]
            lines.append(f'| {idx} | {tag} | {code} | {name} | {price_s} | {pe_s} | {pb_s} | {change_s} | {turnover_s} | {mcap_s} |')
        if idx >= 15:
            break

    if idx == 0:
        lines.append('| - | - | - | 所有策略均无候选 | - | - | - | - | - | - |')

    return '\n'.join(lines)


def _run_daily_signal_scan(mode: str, job_name: str):
    """通用 wrapper：调用 scripts/daily_signal_scan.py 的指定 mode

    用 subprocess 隔离调用，避免 streamlit 进程跟脚本环境冲突
    """
    if _skip_if_not_trading(job_name):
        return
    started = datetime.now().isoformat()
    import subprocess
    project_dir = _bootstrap.ROOT
    script_path = os.path.join(project_dir, 'scripts', 'daily_signal_scan.py')
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    try:
        proc = subprocess.run(
            [sys.executable, script_path, mode],
            cwd=project_dir, env=env, capture_output=True, text=True, timeout=600,
            encoding='utf-8', errors='replace',
        )
        out = (proc.stdout or '') + '\n' + (proc.stderr or '')
        # returncode=0 但完全无输出 → 脚本可能静默崩溃/未产出,不当成功(避免"邮件没发也无告警")
        if proc.returncode == 0 and out.strip():
            status, tail = 'success', out[-500:]
        elif proc.returncode == 0:
            status, tail = 'error', f'returncode=0 but no output (mode={mode})'
        else:
            status, tail = 'error', (out[-500:] if out.strip() else f'returncode={proc.returncode}, no output')
        _log_run(job_name, status, error=tail, started_at=started,
                 finished_at=datetime.now().isoformat())
    except subprocess.TimeoutExpired:
        _log_run(job_name, 'error', error='timeout 600s',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job_name, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_noon_report():
    """📊 午盘简报 — 12:00 推送邮件/Webhook"""
    _run_daily_signal_scan('noon_report', 'noon_report')


def task_morning_strategy():
    """📊 每日晨间市场报告 (08:30)

    同 overnight_ai_strategy 原逻辑，更名为 morning_strategy，
    时间改为 08:30（代替原 dragon_tiger_report 时段），
    新增新闻简报模块。如果内容过长，自动拆分为多条 QQ 消息。
    """
    job = 'morning_strategy'
    # 非交易日跳过(节假日感知,见 _is_trading_day)
    if _skip_if_not_trading(job):
        return

    started = datetime.now().isoformat()
    try:
        from datetime import timedelta
        # 找上个交易日(节假日感知:回退到最近的交易日,跳过周末+法定假日)
        yesterday = datetime.now() - timedelta(days=1)
        for _ in range(15):  # 最多回退 15 天(覆盖春节/国庆长假),兜底防死循环
            if _is_trading_day(yesterday):
                break
            yesterday -= timedelta(days=1)
        lookback_date = yesterday.strftime('%Y-%m-%d')

        # ─── 1. 龙虎榜数据（昨日） — 增强版 ───
        dragon_tiger_summary = '（无数据）'
        dragon_tiger_detailed = ''
        try:
            dt_data = datahub.dragon_tiger_detail(lookback_date)
            if dt_data:
                # AI prompt 用简版摘要
                lines = []
                for i, row in enumerate(dt_data[:15], 1):
                    code = row.get('SECURITY_CODE', '')
                    name = row.get('SECURITY_NAME_ABBR', '')[:8]
                    net = (row.get('BILLBOARD_NET_AMT') or 0) / 10000
                    chg = round(float(row.get('CHANGE_RATE') or 0), 2)
                    if abs(net) > 100:
                        lines.append(f'  {code} {name} 净{net:.0f}万 涨{chg}%')
                dragon_tiger_summary = '\n'.join(lines) if lines else '（无数据）'

                # 详细版（替换独立龙虎榜日报）
                dt_lines = [f'🐉 龙虎榜 ({len(dt_data)}条)', '']
                dt_lines.append('🏆 净买入 TOP 10')
                for i, row in enumerate(dt_data[:10], 1):
                    code = row.get('SECURITY_CODE', '')
                    name = row.get('SECURITY_NAME_ABBR', '')
                    net = (row.get('BILLBOARD_NET_AMT') or 0) / 10000
                    buy = (row.get('BILLBOARD_BUY_AMT') or 0) / 10000
                    sell = (row.get('BILLBOARD_SELL_AMT') or 0) / 10000
                    chg = round(float(row.get('CHANGE_RATE') or 0), 2)
                    reason = (row.get('EXPLANATION') or '')[:20]
                    # A 股惯例:红涨绿跌, 净流入大额 → 红(利好), 净流出 → 绿
                    emoji = '🔴' if net > 5000 else ('⚪' if net > 0 else '🟢')
                    dt_lines.append(f'  {i}. {code} {name} {emoji}净{net:.0f}万 涨{chg}% — {reason}')
                # 净卖出 TOP 5
                sorted_sell = sorted(dt_data, key=lambda x: (x.get('BILLBOARD_NET_AMT') or 0))
                dt_lines.append('')
                dt_lines.append('📉 净卖出 TOP 5')
                for i, row in enumerate(sorted_sell[:5], 1):
                    code = row.get('SECURITY_CODE', '')
                    name = row.get('SECURITY_NAME_ABBR', '')
                    net = (row.get('BILLBOARD_NET_AMT') or 0) / 10000
                    # A 股惯例:净卖出 = 利空 = 绿
                    dt_lines.append(f'  {i}. {code} {name} 🟢净{net:.0f}万')
                dragon_tiger_detailed = '\n'.join(dt_lines)
        except Exception as e:
            dragon_tiger_summary = f'(拉取失败: {e})'

        # ─── 2. 美股 3 大指数（直接 Yahoo V8 API，避免 yfinance 限速） ───
        us_summary = '（无数据）'
        try:
            import requests as _yr
            _h = {'User-Agent': 'Mozilla/5.0'}
            import config as _cfg; _p = _cfg.PROXIES
            _tickers = {'%5EDJI': '道琼斯', '%5EGSPC': '标普500', '%5EIXIC': '纳斯达克'}
            lines = []
            for sym, name in _tickers.items():
                try:
                    resp = _yr.get(
                        f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=3d&interval=1d',
                        headers=_h, proxies=_p, timeout=10)
                    if resp.status_code == 200:
                        d = resp.json()
                        c = d['chart']['result'][0]['indicators']['quote'][0]['close']
                        c = [x for x in c if x is not None]
                        if len(c) >= 2:
                            last, prev = c[-1], c[-2]
                            chg = (last - prev) / prev * 100
                            lines.append(f'  {name}: {last:.2f} ({chg:+.2f}%)')
                except Exception:
                    pass
            if lines:
                us_summary = '\n'.join(lines)
        except Exception as e:
            us_summary = f'(拉取失败: {e})'

        # ─── 3. 隔夜新闻 ───
        news_summary = '（无数据）'
        try:
            news = datahub.market_news(15)
            if news:
                lines = []
                for n in news[:10]:
                    title = (n.get('title') or n.get('content', ''))[:60]
                    t = n.get('time') or ''
                    lines.append(f'  [{t}] {title}')
                news_summary = '\n'.join(lines)
        except Exception as e:
            news_summary = f'(拉取失败: {e})'

        # ─── 4. 北向资金近 5 日 ───
        north_summary = '（无数据）'
        try:
            rows = datahub.north_flow(5)
            if rows:
                lines = []
                for r in rows[:5]:
                    net = (r.get('net_hgt', 0) or 0) + (r.get('net_sgt', 0) or 0)
                    lines.append(f"  {r.get('trade_date', '')}: 净流入 {net/100000000:.2f}亿")
                north_summary = '\n'.join(lines)
        except Exception as e:
            north_summary = f'(拉取失败: {e})'

        # ─── 5. 当日强势股 + 题材热度榜（同花顺热点） ───
        hot_summary = '（无数据）'
        themes_summary = '（无数据）'
        try:
            from agent_tool_groups import _aggregate_hot_themes
            hot_df = datahub.hot_stocks(lookback_date)
            if hot_df is not None and hasattr(hot_df, 'empty') and not hot_df.empty:
                lines = []
                for _, r in hot_df.head(10).iterrows():
                    code = r.get('代码', r.get('code', ''))
                    name = r.get('名称', r.get('name', ''))
                    pct = r.get('涨幅%', r.get('zhangfu', 0))
                    reason = r.get('题材归因', r.get('reason', ''))[:50]
                    lines.append(f'  {code} {name} +{pct}% — {reason}')
                hot_summary = '\n'.join(lines)
                themes = _aggregate_hot_themes(hot_df, top_n=15)
                if themes:
                    themes_summary = '\n'.join(f"  {t['theme']} ({t['count']} 只)" for t in themes)
        except Exception as e:
            hot_summary = f'(拉取失败: {e})'

        # ─── 5c. A股大盘指数 + 板块强弱（原晨报 briefing 并入） ───
        cn_index_summary = '（无数据）'
        sector_summary = '（无数据）'
        try:
            import briefing as _brief
            _mkt = _brief._market()
            if _mkt.get('indices'):
                cn_index_summary = '  '.join(f"{x['name']}{x['v']}" for x in _mkt['indices'])
            parts = []
            if _mkt.get('sector_top'):
                parts.append('强势: ' + '、'.join(f"{s['板块']}{s['涨跌幅']}%" for s in _mkt['sector_top']))
            if _mkt.get('sector_bottom'):
                parts.append('弱势: ' + '、'.join(f"{s['板块']}{s['涨跌幅']}%" for s in _mkt['sector_bottom']))
            if parts:
                sector_summary = '\n'.join(parts)
        except Exception as e:
            cn_index_summary = f'(拉取失败: {e})'

        # ─── 5d. 持仓逐只扫描（共用 _scan_holdings_with_snapshot,零逐只接口）───
        # 仅供 AI 第8维研判;详细买卖列表已移至 09:50 早盘持仓分析推送
        hold_summary = '（无持仓扫描数据）'
        try:
            _scans = _scan_holdings_with_snapshot()
            _sell = sorted([s for s in _scans if s['sell_score'] > 0],
                           key=lambda x: x['sell_score'], reverse=True)[:5]
            _buy = [s for s in _scans if s['buy_signal']][:8]
            _hl = []
            if _sell:
                _hl.append('建议关注卖出: ' + '; '.join(
                    f"{s['name']}{s['code']}风险分{s['sell_score']}({'/'.join(s['sell_reasons'])}"
                    + (f",浮盈{s['pnl']}%" if s['pnl'] is not None else '') + ')' for s in _sell))
            if _buy:
                _hl.append('出现买点: ' + '; '.join(
                    f"{s['name']}{s['code']}({s['buy_reason']})" for s in _buy))
            # 仓位层(position_sizer):结构诊断+约束建议,连同市场环境一起喂给 AI
            try:
                from analysis.position_sizer import analyze as _ps_analyze, format_for_ai as _ps_fmt
                _regime = None
                try:
                    from analysis.strategy_genome import get_strategy_intelligence
                    _mk = get_strategy_intelligence(days=7).get('market', [])
                    _regime = next((m.get('market_regime') for m in _mk if m.get('market_regime')), None)
                except Exception:
                    pass
                _ps_text = _ps_fmt(_ps_analyze(_scans, regime=_regime))
                if _ps_text:
                    _hl.append(_ps_text)
            except Exception:
                pass
            if _hl:
                hold_summary = '\n'.join(_hl)
            elif _scans:
                hold_summary = f'扫描 {len(_scans)} 只持仓(盘后快照),无显著买卖信号'
        except Exception as e:
            hold_summary = f'(扫描失败: {e})'

        # ─── 5e. 昨日收益（原 08:50 morning_pnl 并入,只读 daily_pnl_snapshots,零接口） ───
        pnl_text = ''
        try:
            from portfolio.daily_pnl import get_pnl as _get_pnl
            _p = _get_pnl()
            if _p:
                pnl_text = (
                    f"💰 昨日收益({_p['snap_date']}): "
                    f"股票({_p['stock_count']}只) {_p['stock_daily_pnl']:+,.0f}元({_p['stock_daily_pct']:+.2f}%) | "
                    f"基金({_p['fund_count']}只) {_p['fund_daily_pnl']:+,.0f}元({_p['fund_daily_pct']:+.2f}%) | "
                    f"合计 {_p['total_daily_pnl']:+,.0f}元({_p['total_daily_pct']:+.2f}%)")
        except Exception:
            pass
        if pnl_text:
            hold_summary = pnl_text + '\n' + hold_summary  # AI 也能看到昨日盈亏

        # ─── 6. 美国宏观面板（FRED API + Yahoo V8 fallback） ───
        fred_summary = '（无数据）'
        try:
            from fred_economic_data import get_fed_snapshot, format_snapshot
            snap = get_fed_snapshot()
            fred_summary = format_snapshot(snap)
        except Exception as e:
            fred_summary = f'(拉取失败: {e})'
        # 如果 FRED 无数据，补充 Yahoo V8 市场指标
        if '无数据' in fred_summary or not fred_summary:
            try:
                import requests as _yr2
                _h2 = {'User-Agent': 'Mozilla/5.0'}
                import config as _cfg2; _p2 = _cfg2.PROXIES
                fallback_lines = []
                for sym, name in [('%5ETNX', '10Y收益率'), ('%5EVIX', 'VIX恐慌指数')]:
                    try:
                        resp = _yr2.get(
                            f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d',
                            headers=_h2, proxies=_p2, timeout=10)
                        if resp.status_code == 200:
                            d = resp.json()
                            c = d['chart']['result'][0]['indicators']['quote'][0]['close']
                            c = [x for x in c if x is not None]
                            if len(c) >= 2:
                                last, prev = c[-1], c[-2]
                                chg = (last - prev) / prev * 100
                                fallback_lines.append(f'  {name}: {last:.2f} ({chg:+.2f}%)')
                    except Exception:
                        pass
                if fallback_lines:
                    fred_summary = '\n'.join(fallback_lines) + '\n  (来源: Yahoo Finance)'
            except Exception:
                pass

        # ─── 7. 拼 prompt 调 AI 一次（优先走 prompt_manager 模板，便于运行时编辑） ───
        prompt_vars = {
            'lookback_date': lookback_date,
            'dragon_tiger_summary': dragon_tiger_summary,
            'us_summary': us_summary,
            'news_summary': news_summary,
            'north_summary': north_summary,
            'hot_summary': hot_summary,
            'themes_summary': themes_summary,
            'fred_summary': fred_summary,
            'cn_index_summary': cn_index_summary,
            'sector_summary': sector_summary,
            'hold_summary': hold_summary,
        }
        try:
            from prompt_manager import render as _render_prompt
            prompt = _render_prompt('overnight_strategy', **prompt_vars)
            # DB 里的旧模板(6维)没有 A股大盘/板块/持仓维度 → 运行时补齐,保证 AI 能看到
            if prompt and 'A股大盘指数' not in prompt:
                prompt += (
                    f"\n\n【补充数据维度】\n"
                    f"【7. A股大盘指数】\n{cn_index_summary}\n\n"
                    f"【7b. 行业板块强弱】\n{sector_summary}\n\n"
                    f"【8. 我的持仓技术扫描（含昨日盈亏/浮盈/破位/缠论信号）】\n{hold_summary}\n\n"
                    f"请在输出 JSON 中额外加两个字段: "
                    f"\"lazy_summary\"(3-4句口语化今日操作要点:大盘基调/该卖谁该留谁/可加谁,像朋友间提醒); "
                    f"\"position_advice\"(针对第8维持仓逐只口语化建议:卖/减/留/可加+一句理由)。"
                )
        except Exception as _pme:
            print(f'[jobs_hub] prompt_manager render 失败，使用硬编码兜底: {_pme}')
            prompt = ''
        if not prompt:
            prompt = (
                f"你是一名资深 A 股策略分析师。请基于以下 8 维数据，综合判断今日 A 股开盘策略。\n\n"
                f"【1. 昨日 ({lookback_date}) 龙虎榜 TOP 15（按净买入排序）】\n{dragon_tiger_summary}\n\n"
                f"【2. 美股隔夜收盘】\n{us_summary}\n\n"
                f"【3. 隔夜国内新闻头条】\n{news_summary}\n\n"
                f"【4. 北向资金近 5 日】\n{north_summary}\n\n"
                f"【5. 昨日强势股 TOP 10】\n{hot_summary}\n\n"
                f"【5b. 题材热度榜 TOP 15】\n{themes_summary}\n\n"
                f"【6. 美国宏观面板】\n{fred_summary}\n\n"
                f"【7. A股大盘指数】\n{cn_index_summary}\n\n"
                f"【7b. 行业板块强弱】\n{sector_summary}\n\n"
                f"【8. 我的持仓技术扫描（含昨日盈亏/浮盈/破位/缠论信号）】\n{hold_summary}\n\n"
                f'请综合以上信息严格按 JSON 输出: '
                f'{{"lazy_summary": "3-4句口语化的今日操作要点(大盘基调/该卖谁该留谁/可加谁,像朋友间提醒,直接说人话)", '
                f'"open_strategy": "...", "external_impact": "...", '
                f'"hot_sectors": [...], "risk_warning": "...", '
                f'"candidate_stocks": [...], '
                f'"position_advice": "针对第8维持仓逐只口语化建议(卖/减/留/可加+一句理由)", "confidence": "高/中/低"}}'
            )

        from deepseek_client import DeepSeekClient
        client = DeepSeekClient()
        messages = [
            {'role': 'system', 'content': '你是资深 A 股策略分析师，擅长综合多维数据给出实操开盘建议。'},
            {'role': 'user', 'content': prompt},
        ]
        raw = client.call_api(messages, max_tokens=2000, call_type='morning')

        # 解析 JSON
        import re
        diagnosis = {'raw_text': raw[:1500] if raw else ''}
        try:
            m = re.search(r'\{[\s\S]*\}', raw or '')
            if m:
                diagnosis = json.loads(m.group())
        except Exception:
            pass

        # ─── 8. 构建各模块 ───
        now_str = (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
        data_date = lookback_date

        # === 模块A: 策略核心（AI 分析） ===
        mod_a = []
        mod_a.append(f'📊 晨间市场报告 — {now_str}')
        if diagnosis.get('lazy_summary'):
            mod_a.append('')
            mod_a.append('【今日一句话】')
            mod_a.append(str(diagnosis['lazy_summary']))
        if pnl_text:
            mod_a.append('')
            mod_a.append(pnl_text)
        if cn_index_summary != '（无数据）':
            mod_a.append('【大盘】' + cn_index_summary)
        mod_a.append('')
        mod_a.append('━━━ 🎯 开盘策略 ━━━')
        mod_a.append(diagnosis.get('open_strategy', '（无）'))
        mod_a.append('')
        mod_a.append('━━━ 🌐 外部影响 ━━━')
        mod_a.append(diagnosis.get('external_impact', '（无）'))
        mod_a.append('')
        mod_a.append('━━━ 🔥 热点板块 ━━━')
        for s in diagnosis.get('hot_sectors', []) or []:
            mod_a.append(f'  • {s}')
        mod_a.append('')
        mod_a.append('━━━ ⚠️ 风险提示 ━━━')
        mod_a.append(diagnosis.get('risk_warning', '（无）'))
        mod_a.append('')
        # 候选股票不再展示(选股统一看 09:45 综合选股);candidate_stocks 仍静默入推荐池(wf_overnight_to_rec)
        if diagnosis.get('position_advice'):
            mod_a.append('━━━ 💼 持仓操作建议 ━━━')
            mod_a.append(str(diagnosis['position_advice']))
            mod_a.append('')
        mod_a.append(f'数据置信度: {diagnosis.get("confidence", "未知")}')

        # === 模块B: 新闻简报 — 使用已采集的 news_summary（不复拉） ===
        mod_b = []
        mod_b.append(f'📰 晨间新闻简报 — {data_date}')
        mod_b.append('')
        if news_summary and news_summary != '（无数据）':
            for line in news_summary.split('\n'):
                mod_b.append(line)
        else:
            mod_b.append('  （暂无新闻）')
        mod_b.append('')
        mod_b.append('━━━ 🌏 美股隔夜 ━━━')
        mod_b.append(us_summary)
        mod_b.append('')
        mod_b.append('━━━ 💰 北向资金 5 日 ━━━')
        mod_b.append(north_summary)

        # === 模块C: 龙虎榜详细数据 ===
        mod_c = []
        mod_c.append(f'🐉 龙虎榜 — {data_date}')
        mod_c.append('')
        if dragon_tiger_detailed:
            # 跳过已有标题行
            dt_lines = dragon_tiger_detailed.split('\n')
            mod_c.extend(dt_lines[2:])  # 跳过 '🐉 龙虎榜 (N条)' 和空行
        else:
            mod_c.append('（无数据）')
        mod_c.append('')
        mod_c.append('━━━ 🔥 昨日强势股 TOP 10 ━━━')
        mod_c.append(hot_summary)
        mod_c.append('')
        mod_c.append('━━━ 🏷️ 题材热度榜 ━━━')
        mod_c.append(themes_summary)
        mod_c.append('')
        mod_c.append('━━━ 🏛️ 宏观面板 (FRED) ━━━')
        mod_c.append(fred_summary)

        # (原模块D"持仓买卖提示"已移至 09:50 morning_portfolio,用开盘后实时价更准;
        #  晨报回到 3 条,AI 的持仓建议仍在模块A的 position_advice)

        # 组合各模块
        modules = [('\n'.join(mod_a), '📊 晨间市场报告'),
                   ('\n'.join(mod_b), '📰 晨间新闻简报'),
                   ('\n'.join(mod_c), '🐉 盘前数据快照')]

        # ─── 模块推送:全部走 notification_router(report→默认QQ);
        #     QQ 整体不通时,合并 3 条为一封邮件兜底(不丢消息也不轰炸) ───
        sent_ok = 0
        try:
            from notification_router import send as _nr_send
            for mod_text, mod_title in modules:
                text = mod_text.strip()
                if not text:
                    continue
                res = _nr_send('report', mod_title, text, fallback=None)
                if any(ok for ok, _ in res.values()):
                    sent_ok += 1
            if sent_ok == 0:
                full_content = '\n\n'.join(f'# {t}\n\n{m}' for m, t in modules)
                _nr_send('report', f'📊 晨间市场报告 — {datetime.now().strftime("%m-%d")}',
                         full_content, only_channels=['email'], fallback=None)
        except Exception as qe:
            print(f'[morning_strategy] 模块推送失败: {qe}')

        # 保存到 PG analysis_records（精简）
        try:
            from database import db
            db.save_analysis(
                symbol='_OVERNIGHT_STRATEGY_',
                stock_name='晨间市场报告',
                period='1d',
                stock_info={'date': lookback_date},
                agents_results={'strategy_agent': {
                    'agent_name': '晨间策略 AI',
                    # 修复:原引用未定义的 content 变量,NameError 被静默吞掉导致从不落库
                    'analysis': '\n'.join(mod_a)[:400],
                }},
                discussion_result={'summary': diagnosis.get('open_strategy', '')},
                final_decision=diagnosis,
            )
        except Exception:
            pass

        # ─── 9. 工作流 A：candidate_stocks → AI 推荐池（开关 wf_overnight_to_rec） ───
        rec_summary = ''
        try:
            from automation_config import is_enabled
            if is_enabled('wf_overnight_to_rec'):
                from ai_recommendation_monitor import save_recommendation, enable_monitor
                inserted = 0
                for s in diagnosis.get('candidate_stocks', []) or []:
                    if not isinstance(s, dict):
                        continue
                    code = s.get('code')
                    if not code:
                        continue
                    try:
                        # C(门控一致性,2026-06-26):A 路对齐 B/final_decision 的盈亏比硬约束 —— 此前晨间
                        # 候选直接入池+监控,无止损/无 R:R 校验(全项目唯一漏网)。现:买入必须有止损
                        # (缺则按进场×0.92兜底),(目标-进场)/(进场-止损)<2:1 → 性价比不足,跳过不入池。
                        _el, _eh = _safe_float(s.get('entry_low')), _safe_float(s.get('entry_high'))
                        _entry = (_el + _eh) / 2 if (_el and _eh) else (_el or _eh)
                        _tp = _safe_float(s.get('target_price'))
                        _is_buy = ('buy' in str(s.get('rating', 'buy')).lower()
                                   or '买' in str(s.get('rating', '')))
                        if _is_buy and _entry:
                            _sl = _safe_float(s.get('stop_loss'))
                            if not _sl or _sl <= 0 or _sl >= _entry:
                                _sl = round(_entry * 0.92, 2)
                                s['stop_loss'] = _sl   # 兜底止损回写,save 时带上
                            if _tp and _sl and _tp > _entry > _sl > 0 and (_tp - _entry) / (_entry - _sl) < 2.0:
                                continue   # 盈亏比 < 2:1,不入池(与 B 路/final_decision 一致)
                        rid = save_recommendation(
                            symbol=str(code),
                            name=s.get('name', ''),
                            source='overnight_strategy',
                            rating=s.get('rating', 'buy'),
                            confidence=diagnosis.get('confidence', '中'),
                            target_price=_safe_float(s.get('target_price')),
                            entry_low=_safe_float(s.get('entry_low')),
                            entry_high=_safe_float(s.get('entry_high')),
                            take_profit=_safe_float(s.get('target_price')),
                            stop_loss=_safe_float(s.get('stop_loss')),
                            reason=s.get('reason', '')[:500],
                        )
                        if rid:
                            enable_monitor(rid)
                            inserted += 1
                    except Exception as e:
                        print(f'[wf_overnight_to_rec] {code} 入库失败: {e}')
                rec_summary = f' wf_to_rec={inserted}'
        except Exception as wfe:
            print(f'[wf_overnight_to_rec] 工作流失败: {wfe}')

        # 后台预热 webui 晨报文件缓存(用户开页只读缓存秒回,不再每次冷算~88s)
        try:
            import briefing
            briefing.cached_briefing(force=True)
        except Exception as _be:
            print(f'[morning_strategy] 晨报缓存预热失败: {_be}')

        _log_run(job, 'success',
                 error=f'candidates={len(diagnosis.get("candidate_stocks", []) or [])}{rec_summary}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def _source_feedback(source: str, days: int = 90, min_samples: int = 10):
    """读取某 source 近 N 天的真实盈亏表现,回喂给决策(闭合反馈环)。

    返回 {n, win_rate, avg_ret, text, conservative}:
      - text: 注入 AI prompt 的一行历史战绩(样本不足则提示"样本不足")
      - conservative: 样本够(≥min_samples)且平均收益明显为负(<-3%) → True,
        调用方应收紧门槛(只收 strong_buy / 降置信 / 不自动监控)。
    失败返回中性值(不影响主流程)。"""
    out = {'n': 0, 'win_rate': None, 'avg_ret': None, 'text': '', 'conservative': False}
    try:
        from ai_evaluation import evaluate_by_source
        res = evaluate_by_source(days=days).get(source)
        if not res or res.sample_size == 0:
            out['text'] = f'(本策略近{days}天无历史战绩样本)'
            return out
        m = res.metrics
        n = m.get('n_with_return', 0)
        wr, ar = m.get('win_rate_pct'), m.get('avg_return_pct')
        out.update(n=n, win_rate=wr, avg_ret=ar)
        out['text'] = (f'本策略近{days}天历史战绩:真实胜率 {wr}%、平均收益 {ar:+}%、'
                       f'盈亏比 {m.get("profit_factor")}(样本 {n});请据此校准信心,战绩差则提高买入门槛。')
        if n >= min_samples and ar is not None and ar < -3:
            out['conservative'] = True
    except Exception as e:
        print(f'[_source_feedback] {source} 读取失败: {e}')
    return out


def _parse_tp_sl(text: str, ref_price=None):
    """从 AI 自由文本里解析 (take_profit, stop_loss, target_price)。

    止盈优先取「止盈」,无则用「目标价」;止损取「止损」。
    有 ref_price 时做合理性校验:止盈须>现价、止损须<现价,且不偏离现价 5 倍以上,否则丢弃。
    """
    import re

    def _grab(labels):
        for lab in labels:
            # 标签后只允许典型分隔符(冒号/约/为/¥/元/空格),数字后不得跟 %(挡"涨幅30%"这类干扰)
            m = re.search(lab + r'[价位]{0,2}[：:约为\s¥元]{0,4}(\d+\.?\d*)(?!\s*[%％])', text)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        return None

    tp = _grab(['止盈', '目标'])
    target = _grab(['目标']) or tp
    sl = _grab(['止损'])
    if ref_price:
        ref = float(ref_price)
        # 合理性:止盈在现价之上且不离谱;止损在现价之下且不离谱
        if tp is not None and not (ref < tp <= ref * 5):
            tp = None
        if sl is not None and not (ref * 0.2 <= sl < ref):
            sl = None
        if target is not None and not (ref * 0.2 <= target <= ref * 5):
            target = None
    return tp, sl, target


def _daily_strategy_scan():
    """🔗 工作流 B：盘后 InStock 10 策略扫描 → 命中股深度 AI 分析 → 入推荐池

    股票池：持仓 + 当日强势股 TOP 30 + 当日龙虎榜 TOP 20（去重）
    命中策略 ≥1 套的股票按命中数排序，对 TOP N 跑 plan_execute AI 分析
    AI 给出 "buy/strong_buy" 评级时入 ai_recommendations + 启用监控
    受开关 wf_daily_strategy_scan 控制（默认开;生产以 DB automation_switches 为准）。
    （2026-06-12 整合:并入 daily_backtest 之后执行——回测进化完基因组,再用最新策略情报扫描,不再独立调度）
    """
    job = 'wf_daily_strategy_scan'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return

    started = datetime.now().isoformat()
    try:
        # 1. 组装股票池（去重）
        pool: dict = {}
        try:
            from portfolio_db import portfolio_db
            for s in portfolio_db.get_all_stocks() or []:
                code = s.get('code') if isinstance(s, dict) else getattr(s, 'code', None)
                name = s.get('name') if isinstance(s, dict) else getattr(s, 'name', '')
                if code:
                    pool[code] = name or ''
        except Exception as e:
            print(f'[wf_daily_strategy_scan] 持仓加载失败: {e}')

        try:
            hot_df = datahub.hot_stocks()
            if hot_df is not None and hasattr(hot_df, 'empty') and not hot_df.empty:
                for _, r in hot_df.head(30).iterrows():
                    code = str(r.get('代码', r.get('code', '')) or '').strip()
                    name = str(r.get('名称', r.get('name', '')) or '').strip()
                    if code and code not in pool:
                        pool[code] = name
        except Exception as e:
            print(f'[wf_daily_strategy_scan] 强势股加载失败(池子可能偏小): {e}')

        try:
            lhb = datahub.dragon_tiger()
            if isinstance(lhb, list):
                for r in lhb[:20]:
                    if isinstance(r, dict):
                        code = str(r.get('stock_code', r.get('代码', '')) or '').strip()
                        name = str(r.get('stock_name', r.get('名称', '')) or '').strip()
                        if code and code not in pool:
                            pool[code] = name
        except Exception as e:
            print(f'[wf_daily_strategy_scan] 龙虎榜加载失败(池子可能偏小): {e}')

        # 并入今日 unified_selection(09:45 综合选股)的 TOP 选股 —— 让早盘多因子/主力选出的候选
        # 也走"策略命中 → AI 深析 → 推荐池 + 决策信号"全流程被追踪(此前它们只零成本到期了结)。
        try:
            _us_added = 0
            for _c in _last_selection_picks():
                if _c and _c not in pool:
                    pool[_c] = ''
                    _us_added += 1
            if _us_added:
                print(f'[wf_daily_strategy_scan] 并入 unified_selection 今日选股 {_us_added} 只')
        except Exception as e:
            print(f'[wf_daily_strategy_scan] unified_selection 池并入失败: {e}')

        if not pool:
            _log_run(job, 'success', error='empty_pool',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        # 2. 对池内逐只跑 InStock 13 策略(基因组最优参数 + 组合新策略)
        from instock_strategy_runner import run_one
        from stock_data import StockDataFetcher
        fetcher = StockDataFetcher()

        scan_results = []
        scan_pool = list(pool.items())
        if len(scan_pool) > 100:   # 截断不再静默:让"被丢弃的候选"可见,排查"为何漏了某票"
            print(f'[wf_daily_strategy_scan] 股票池 {len(scan_pool)} 超上限,只扫前 100(丢弃 {len(scan_pool)-100})')
        scan_fail = 0
        for code, name in scan_pool[:100]:  # 上限 100 防过载
            try:
                df = fetcher.get_stock_data(code, '2y', adjust='qfq')  # 盘后策略扫描(形态)用前复权
                if df is None or len(df) == 0:
                    scan_fail += 1
                    continue
                r = run_one(code, df, name=name, evolved=True)
                if r['matched_count'] > 0:
                    scan_results.append(r)
            except Exception as _e:
                scan_fail += 1
                continue
        if scan_fail:   # 批量取数失败不再无声:>1/3 失败往往是数据源抖动/被墙,值得告警排查
            print(f'[wf_daily_strategy_scan] 扫描取数失败 {scan_fail}/{min(len(scan_pool),100)} 只'
                  + ('(失败过半,疑数据源异常)' if scan_fail > min(len(scan_pool), 100) / 2 else ''))

        scan_results.sort(key=lambda x: x['matched_count'], reverse=True)
        top_n = scan_results[:5]  # 仅 TOP 5 跑 AI

        # 3. AI 深度分析（plan_execute）—— 闭合反馈环:注入本策略历史真实战绩 + 决策信号后验
        fb = _source_feedback('wf_daily_strategy_scan')
        try:
            from decision_signal import feedback_text as _ds_feedback
            _ds_fb = _ds_feedback(days=120)
        except Exception:
            _ds_fb = ''
        ai_inserted = 0
        for r in top_n:
            try:
                from agent_router import run as agent_run
                from ai_recommendation_monitor import save_recommendation, enable_monitor, _current_price
                hits = ', '.join([m['cn'] for m in r['matched']])
                q = (f'{r["symbol"]} {r["name"]} 当日命中策略：{hits}。'
                     f'请全面分析其投资价值（技术/资金/基本面/情绪/筹码），'
                     f'给出 buy/strong_buy/hold 评级 + 目标价 + 止损价。')
                if fb['text']:
                    q += f'\n【历史反馈】{fb["text"]}'
                if _ds_fb:   # 决策信号后验回喂(全 source 各动作历史方向命中率),闭合 decision_signal 反馈环
                    q += f'\n{_ds_fb}'
                # 注入策略基因组情报（跨股横截面 + 个股适配度）
                try:
                    from analysis.strategy_genome import get_strategy_intelligence, format_intelligence_for_ai
                    intel = get_strategy_intelligence(stock_code=r['symbol'], days=60)
                    intel_text = format_intelligence_for_ai(intel)
                    if intel_text and '尚无策略情报' not in intel_text:
                        q += f'\n\n{intel_text}'
                except Exception:
                    pass
                ar = agent_run(q, symbol=r['symbol'], prefer_mode='plan_execute')
                answer = ar.get('answer', '')
                # 买入判定:弃用脆弱的 'buy' 子串(会把"不建议买/avoid buy/选项回显 buy/strong_buy/hold"全误判为买入,
                # 污染推荐池与真实胜率反馈)。plan_execute 输出以中文结论为主 → 用中文正向词 + 否定优先门。
                import re as _re2
                _neg = bool(_re2.search(r'不建议买|不宜买|不要买|回避|规避|观望|暂不|avoid', answer, _re2.I))
                is_strong = bool(_re2.search(r'强烈买入|强买|strong[\s_]?buy', answer, _re2.I))
                is_buy = (not _neg) and (is_strong or bool(
                    _re2.search(r'买入|买进|逢低买|逢低吸|建仓|增持|加仓', answer)))
                ref = _current_price(r['symbol'])
                tp, sl, tgt = _parse_tp_sl(answer, ref)

                # ── 决策信号(统一信号层):捕获 TOP5 的**完整结论**(含非买入),source_type='selection' ──
                # 与推荐池分工:推荐池只装被监控的买入+真实P&L;信号层是"广度索引",存全 8 态结论 + 方向后验,
                # 让"扫到但 AI 说别买"的否决也可回溯,并支持 selection vs analysis 跨来源胜率对比。失败不影响主流程。
                try:
                    from decision_signal import create_signal, normalize_action
                    _act = 'buy' if is_buy else normalize_action('hold', answer)
                    _score = 85 if is_strong else (70 if is_buy else (40 if _act == 'avoid' else 50))
                    create_signal(
                        code=r['symbol'], name=r['name'], action=_act,
                        source_type='selection', source_ref=f'strat:{hits[:60]}',
                        confidence=('低' if fb['conservative'] else '中'), score=_score,
                        horizon='swing', ref_price=ref,
                        stop_loss=sl if is_buy else None,
                        target_price=(tp or tgt) if is_buy else None,
                        reason=f'命中 {len(r["matched"])} 策略({hits[:100]}) + AI 研判')
                except Exception as _se:
                    print(f'[wf_daily_strategy_scan] 决策信号落库跳过: {type(_se).__name__}: {str(_se)[:60]}')

                # 反馈门槛:本策略历史战绩差(conservative)时,只收 strong_buy 且不自动启用监控
                if fb['conservative'] and not is_strong:
                    continue
                if is_buy:
                    # 买入无止损 → 按默认止损(现价×0.92,即8%)兜底:确保有风险边界 + 监控能触发止损,杜绝"无止损绕过盈亏比"
                    if ref and (not sl or sl <= 0 or sl >= ref):
                        sl = round(ref * 0.92, 2)
                    # 盈亏比硬约束:目标/止损齐全且 (目标-现价)/(现价-止损) < 2:1 → 性价比不足,不入库
                    if ref and tp and sl and tp > ref > sl > 0:
                        if (tp - ref) / (ref - sl) < 2.0:
                            continue
                    rid = save_recommendation(
                        symbol=r['symbol'], name=r['name'],
                        source='wf_daily_strategy_scan',
                        rating='strong_buy' if is_strong else 'buy',
                        confidence='低' if fb['conservative'] else '中',
                        target_price=tgt, take_profit=tp, stop_loss=sl, ref_price=ref,
                        reason=f'命中 {len(r["matched"])} 策略({hits[:120]}) + AI 综合分析',
                    )
                    if rid:
                        # conservative 的买入也启监控:让带 tp/sl 的买入统一走"监控止盈止损了结"分支,
                        # 否则它落入 candidate 分支只按 90 天浮盈了结、忽略止损 → 同 source 两套了结口径、
                        # 扭曲 evaluate_by_source 真实胜率(_source_feedback 的输入)。监控≠推送(AI推荐本就不推)。
                        enable_monitor(rid)
                        ai_inserted += 1
            except Exception as e:
                print(f'[wf_daily_strategy_scan] {r["symbol"]} AI 失败: {e}')

        # ─── 推送摘要到通知通道 ───
        try:
            lines = [
                f'🔍 盘后策略扫描报告 — {datetime.now().strftime("%Y-%m-%d %H:%M")}',
                '',
                f'股票池: {len(pool)} 只 | 命中策略: {len(scan_results)} 只 | 新AI推荐: {ai_inserted} 只',
                '',
            ]
            if scan_results:
                lines.append('━━━ 命中策略 TOP 5 ━━━')
                for i, r in enumerate(scan_results[:5], 1):
                    matched_cn = [m["cn"] for m in r.get("matched", [])]
                    lines.append(f"{i}. {r['symbol']} {r['name']} — 命中 {r['matched_count']} 策略: {', '.join(matched_cn)}")
            if ai_inserted > 0:
                lines.extend(['', f'✅ {ai_inserted} 只入选AI推荐池（已启用监控）'])
            from notification_router import send
            send('report', '🔍 盘后策略扫描', '\n'.join(lines))
        except Exception as ne:
            print(f'[wf_daily_strategy_scan] 推送失败: {ne}')

        _log_run(job, 'success',
                 error=f'pool={len(pool)} matched={len(scan_results)} ai_inserted={ai_inserted}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def _safe_float(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _daily_candidate_pool():
    """🔗 工作流 E：每日扫候选池 (你的个人策略口味)
    （2026-06-12 整合:并入 unified_selection 之后执行,不再独立调度;开关 wf_daily_candidate_pool 仍有效）

    筛选条件 (来自 user_strategy_config，可在 UI 调)：
      价格 ≤ price_max (默认 20 元)
      + 非 ST / 非退市风险
      + 资产负债率 ≤ 70%（pywencai 过滤）
      + 基本面打分 ≥ fundamental_min_score
      + 所属概念在最近 5 日强势榜 TOP N (hot_sector_top_n)
      + 同时满足任一买点：
        - 当日跌幅 > drop_trigger_pct_today
        - 离 60 日低点 ≤ short_term_low_pct
        - 离 1 年低点 ≤ historical_low_pct
        - TA-Lib 看涨反转形态(需放量确认)
      + ⭐企稳确认(防接飞刀)：价格收回 5 日线 或 有放量反转;
        纯"低位/下跌"而仍在 MA5 下、无放量反转 → 剔除
    推送：当日候选 TOP 10（按基本面打分排序）
    """
    job = 'wf_daily_candidate_pool'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return

    started = datetime.now().isoformat()
    try:
        import user_strategy_config as cfg
        price_max = cfg.get('price_max', 20.0)
        fund_min = cfg.get('fundamental_min_score', 50.0)
        short_low_pct = cfg.get('short_term_low_pct', 5.0)
        hist_low_pct = cfg.get('historical_low_pct', 10.0)
        drop_today = cfg.get('drop_trigger_pct_today', 2.0)

        # 1. 获取候选股票池 — pywencai(主) → 东财 dataapi(兜底)
        base = None
        try:
            from data.pywencai_safe import pywencai_get
            query = (f'股价小于{price_max}，'
                     f'非st，非退市风险股，资产负债率小于70%，'
                     f'按当日成交额由大到小排名')
            base = pywencai_get(query, timeout=90)
            if base is not None and len(base) == 0:
                base = None
        except Exception:
            print('[wf_daily_candidate_pool] pywencai 不可用，尝试 dataapi 兜底')
            base = None

        if base is None:
            # dataapi fallback: 低价 + 非 ST (东财选股器不支负债率过滤)
            try:
                from selection.data_source_config import fetch_stocks_dataapi
                res = fetch_stocks_dataapi(price_max=price_max, top_n=200)
                if res.get('success') and res.get('data'):
                    base = res['data']
                    print(f'[wf_daily_candidate_pool] dataapi 兜底成功: {len(base)} 只')
            except Exception as e:
                print(f'[wf_daily_candidate_pool] dataapi 也失败: {e}')

        if not base or (isinstance(base, (list, tuple)) and len(base) == 0):
            _log_run(job, 'error', error='pywencai + dataapi 均失败',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        # 2. 拿强势板块名单（行业 + 概念）TOP N
        hot_sectors = set()
        try:
            hot_n = int(cfg.get('hot_sector_top_n', 30))
            for sector_type in ('industry', 'concept'):
                rows = datahub.sector_fund_flow(sector_type, hot_n)
                for r in (rows or [])[:hot_n]:
                    if r.get('name'):
                        hot_sectors.add(r['name'])
        except Exception as e:
            print(f'[wf_daily_candidate_pool] 强势板块获取失败: {e}')

        # 3. 对每只候选股做"买点 + 基本面 + 板块"筛选
        from fundamental_scoring import score_one
        from stock_data import StockDataFetcher
        from pattern_recognition import PatternDetector

        fetcher = StockDataFetcher()
        det = PatternDetector()
        BULLISH_REVERSAL = {
            'hammer', 'inverted_hammer', 'morning_star', 'morning_doji_star',
            'engulfing_bull', 'piercing', 'three_white_soldiers',
            'three_inside_up', 'three_outside_up', 'dragonfly_doji',
            'abandoned_baby_bull',
        }

        candidates = []
        sector_err = scan_err = knife_skip = 0  # 板块/买点失败数 + 无企稳确认被剔除数(末尾汇总)
        # 兼容 pywencai(DataFrame) 和 dataapi(list[dict])
        # 2026-06-25: 200→60。候选池盘中(09:45)逐只拉 qfq K线(东财主源)是雪崩高危点,
        # 200 只×限流 ≈ 数百秒;粗筛 60 只足够(后续还有形态/企稳过滤),且 qfq 已有 sina 真非东财兜底。
        if isinstance(base, (list, tuple)):
            rows_iter = base[:60]
        else:
            rows_iter = (row for _, row in base.head(60).iterrows())
        for row in rows_iter:
            if isinstance(row, dict):
                code = str(row.get('code', row.get('股票代码', '')) or '').strip()
                name = str(row.get('name', row.get('股票简称', '')) or '').strip()
            else:
                code = str(row.get('股票代码', row.get('code', '')) or '').strip()
                name = str(row.get('股票简称', row.get('name', '')) or '').strip()
            if not code:
                continue

            # 板块归属（如果该股能映射到强势板块，加分）
            in_hot = False
            try:
                blocks = datahub.concept_blocks(code)
                if blocks and isinstance(blocks, dict):
                    own = (blocks.get('industry', []) or []) + (blocks.get('concept', []) or [])
                    for tag in own:
                        if isinstance(tag, dict):
                            tag = tag.get('name', '')
                        if tag and tag in hot_sectors:
                            in_hot = True; break
            except Exception:
                sector_err += 1

            # 买点判断
            buy_points = []
            try:
                df = fetcher.get_stock_data(code, '1y', adjust='qfq')  # 买点判断(均线/形态)用前复权
                if df is None or len(df) < 60:
                    continue
                close_col = 'Close' if 'Close' in df.columns else 'close'
                closes = df[close_col].astype('float64')
                last = float(closes.iloc[-1])
                yest = float(closes.iloc[-2]) if len(closes) >= 2 else last
                today_chg = (last - yest) / yest * 100 if yest else 0

                # —— 趋势/量能确认所需指标 ——
                ma5 = float(closes.tail(5).mean())
                turned_up = last >= ma5  # 价格收回 5 日线 → 短期动量转头(非自由落体)
                vol_col = 'Volume' if 'Volume' in df.columns else ('volume' if 'volume' in df.columns else None)
                vol_ok = True  # 无量数据时不卡
                if vol_col is not None and len(df) >= 20:
                    vols = df[vol_col].astype('float64')
                    avg_vol20 = float(vols.tail(20).mean())
                    vol_ok = avg_vol20 > 0 and float(vols.iloc[-1]) >= avg_vol20  # 当日量 ≥ 20日均量

                if today_chg <= -drop_today:
                    buy_points.append(f'当日跌{abs(today_chg):.1f}%')

                low_60 = float(closes.tail(60).min())
                if low_60 > 0 and (last - low_60) / low_60 * 100 <= short_low_pct:
                    buy_points.append('短期低位')

                low_1y = float(closes.min())
                if low_1y > 0 and (last - low_1y) / low_1y * 100 <= hist_low_pct:
                    buy_points.append('历史低位')

                has_reversal = False
                if det.available and len(df) >= 120:
                    r = det.detect_all(df, lookback=2)
                    for pid, info in (r or {}).items():
                        if pid == 'support_resistance' or not isinstance(info, dict):
                            continue
                        # 反转形态需放量确认(缩量反转多为假反弹)
                        if (info.get('found') and pid in BULLISH_REVERSAL
                                and info.get('days_ago', 99) <= 1 and vol_ok):
                            buy_points.append(f"反转形态:{info.get('name', pid)}(放量)")
                            has_reversal = True
                            break

                # —— 强确认:缩量回踩 / 底部放量(借 strategy_signals,更严谨的反转判定)——
                strong_confirm = False
                try:
                    from strategy_signals import shrink_pullback, bottom_volume
                    if shrink_pullback(df).get('signal'):
                        buy_points.append('✓缩量回踩'); strong_confirm = True
                    elif bottom_volume(df).get('signal'):
                        buy_points.append('✓底部放量'); strong_confirm = True
                except Exception:
                    pass

                # —— 企稳确认门槛:纯"低位/下跌"而价格仍在 5 日线下、无放量反转、无强确认 → 接飞刀,剔除 ——
                if buy_points:
                    if turned_up:
                        buy_points.append('✓站上MA5')
                    if not (turned_up or has_reversal or strong_confirm):
                        knife_skip += 1
                        continue
            except Exception:
                scan_err += 1
                continue

            if not buy_points:
                continue

            # 基本面打分
            fv = score_one(code) or {}
            fs = fv.get('score')
            if fs is None or fs < fund_min:
                continue

            candidates.append({
                'code': code, 'name': name,
                'price': last,
                'today_chg_pct': today_chg,
                'buy_points': buy_points,
                'fundamental_score': fs,
                'fundamental_grade': fv.get('grade', '?'),
                'in_hot_sector': in_hot,
            })
            if len(candidates) >= 30:
                break

        candidates.sort(key=lambda x: (x['in_hot_sector'], x['fundamental_score']), reverse=True)
        top = candidates[:10]

        # 4. 推送
        if top:
            lines = [f'🎯 每日候选池 — {datetime.now().strftime("%Y-%m-%d")}',
                     f'共 {len(candidates)} 只过审，按基本面 + 板块强弱推 TOP 10', '']
            for i, x in enumerate(top, 1):
                hot_tag = ' 🔥' if x['in_hot_sector'] else ''
                lines.append(
                    f"{i}. {x['code']} {x['name']}  ¥{x['price']:.2f}{hot_tag}\n"
                    f"   基本面: {x['fundamental_grade']} ({x['fundamental_score']})  "
                    f"今日 {x['today_chg_pct']:+.2f}%\n"
                    f"   买点: {', '.join(x['buy_points'])}"
                )
            report = '\n'.join(lines)
            try:
                from notification_router import send
                send('report', '🎯 每日候选池', report)
            except Exception:
                print(report)

        _log_run(job, 'success',
                 error=f'pool_total={len(candidates)} pushed={len(top)} '
                       f'knife_skip={knife_skip} sector_err={sector_err} scan_err={scan_err}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def _position_profit_check():
    """🔗 工作流 H：持仓减仓信号（方案 A：30/60/100 阶梯 + MA 保护）
    （2026-06-12 整合:并入 afternoon_portfolio 尾盘执行,不再独立调度;开关 wf_position_profit_check 仍有效）
    - 涨 ≥ 30/60/100% 各推一次"减 30%"建议
    - 跌破 MA20 → "减 50%" 警告
    - 跌破 MA60 → "清仓" 警告
    用户实际减仓后下次扫描会基于新持仓重算。
    """
    job = 'wf_position_profit_check'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return

    started = datetime.now().isoformat()
    try:
        from position_profit_taker import evaluate_all, format_alert
        items = evaluate_all()
        if items:
            text = format_alert(items)
            try:
                from notification_router import send
                send('alert', '💰 持仓减仓信号', text)
            except Exception:
                print(text)
        critical = sum(1 for x in items
                       if any(a['severity'] == 'critical' for a in x['actions']))
        warning = sum(1 for x in items
                      if any(a['severity'] == 'warning' for a in x['actions']))
        _log_run(job, 'success',
                 error=f'triggered={len(items)} critical={critical} warning={warning}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def _position_guard_check():
    """🔗 工作流 G：盘中扫持仓加仓信号（仅交易时段执行）
    （2026-06-12 整合:并入 stock_monitor_check 每30分钟执行,不再独立调度;开关 wf_position_guard_check 仍有效）

    对持仓股做加仓审核 (position_guardian.evaluate_all_triggered)：
      ✅ 通过 → 推"建议加仓"
      ⚠️ 拒绝 → 推"加仓警告"（反建议止损）
    """
    job = 'wf_position_guard_check'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    now = datetime.now()
    # datetime.now() 已是 CST（TZ=Asia/Shanghai）
    minutes = now.hour * 60 + now.minute
    if minutes < (9 * 60 + 30) or minutes > (15 * 60):
        return

    started = datetime.now().isoformat()
    try:
        from position_guardian import evaluate_all_triggered, format_alert
        # 限市值 top15 + 跳过慢的基本面评分(2026-06-25):原无 limit+含基本面,外网全挂时全持仓逐只
        # 吃满 quote60s+kline135s+基本面 → 加仓审核 1813s 拖垮 stock_monitor_check + 推残缺信号。
        items = evaluate_all_triggered(limit=15, with_fundamental=False)
        if items:
            text = format_alert(items)
            try:
                from notification_router import send
                send('alert', '📊 持仓加仓信号', text)
            except Exception:
                print(text)
        _log_run(job, 'success',
                 error=f"triggered={len(items)} approve={sum(1 for x in items if x['verdict']=='approve')} "
                       f"reject={sum(1 for x in items if x['verdict']=='reject')}",
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_factor_collection():
    """📊 每日收盘后采集因子快照 — OHLCV + 技术指标 + 估值打分"""
    job = 'factor_collection'
    if _skip_if_not_trading(job):   # 非交易日无新行情:跳过,免采集重复/陈旧因子快照
        return
    _wait_kline_prefetch(job)       # F: 等 kline_prefetch 焐完 K线+因子缓存再采集(读暖缓存,不冷拉)
    started = datetime.now().isoformat()
    try:
        import factor_collector
        factor_collector.collect(do_score=True)
        _log_run(job, 'success', started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started, finished_at=datetime.now().isoformat())


def task_weekly_backtest():
    """🔗 工作流 C：周日晚 InStock 10 策略回测 → 推送"最有效策略"周报

    池：持仓 + 强势股 TOP 20（轻量化避免太重）
    每只跑 10 套策略过去 30 天回测，汇总胜率 → 推送 TOP 5 策略
    受开关 wf_weekly_backtest 控制（默认开）。
    """
    job = 'wf_weekly_backtest'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass

    started = datetime.now().isoformat()
    try:
        from datetime import timedelta
        from backtest_engine import backtest_batch
        from instock_strategy_runner import STRATEGIES

        # 1. 组装股票池
        stocks = []
        try:
            from portfolio_db import portfolio_db
            for s in portfolio_db.get_all_stocks() or []:
                code = s.get('code') if isinstance(s, dict) else getattr(s, 'code', None)
                name = s.get('name') if isinstance(s, dict) else getattr(s, 'name', '')
                if code:
                    stocks.append((code, name or ''))
        except Exception:
            pass

        try:
            hot_df = datahub.hot_stocks()
            if hot_df is not None and hasattr(hot_df, 'empty') and not hot_df.empty:
                for _, r in hot_df.head(20).iterrows():
                    code = str(r.get('代码', r.get('code', '')) or '').strip()
                    name = str(r.get('名称', r.get('name', '')) or '').strip()
                    if code and not any(s[0] == code for s in stocks):
                        stocks.append((code, name))
        except Exception:
            pass

        if not stocks:
            _log_run(job, 'success', error='empty_pool',
                     started_at=started, finished_at=datetime.now().isoformat())
            return

        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

        # 取基因组进化后的 live 最优参数(2026-06-12:原来评的是出厂默认,跟实盘用的参数脱节)
        live_params = {}
        try:
            from analysis.strategy_genome import get_live_strategy_set
            live_params = (get_live_strategy_set() or {}).get('base', {})
        except Exception:
            pass

        # 2. 逐策略回测，汇总胜率
        results = []
        for sid in STRATEGIES.keys():
            try:
                bp = live_params.get(sid) or None
                hd = int((bp or {}).get('hold_days') or 10)
                # 双收益:带 8% 止损 / 15% 止盈,评估"有纪律 vs 持有到期"的差异
                r = backtest_batch(stocks, sid, start_date, end_date, hold_days=hd,
                                   stop_pct=8, target_pct=15, params=bp)
                summary = r.get('summary', {})
                if summary.get('count', 0) >= 3:  # 至少 3 个样本才有统计意义
                    results.append({
                        'strategy_id': sid,
                        'cn': STRATEGIES[sid]['cn'],
                        'count': summary['count'],
                        'win_rate': summary['win_rate'],
                        'avg_ret_pct': summary['avg_ret_pct'],
                        'max_dd_pct': summary['avg_max_dd_pct'],
                        'disc_ret_pct': summary.get('avg_ret_disciplined_pct'),
                        'disc_impact': summary.get('discipline_impact_pct'),
                        'stop_rate': summary.get('stop_trigger_rate'),
                        'tgt_rate': summary.get('target_trigger_rate'),
                    })
            except Exception as e:
                print(f'[wf_weekly_backtest] {sid} 失败: {e}')

        # 3. 按胜率排序 + 推送
        results.sort(key=lambda x: (x['win_rate'], x['avg_ret_pct']), reverse=True)
        lines = [f'📊 InStock 10 策略 30 天回测周报 — {end_date}',
                 f'股票池: {len(stocks)} 只  期间: {start_date} ~ {end_date}  持有: 10 天', '']
        if results:
            lines.append('━━━ 最有效策略 TOP 5(含8%止损/15%止盈纪律对比)━━━')
            for i, r in enumerate(results[:5], 1):
                lines.append(f"{i}. {r['cn']:>12s} 胜率={r['win_rate']}% "
                             f"avg_ret={r['avg_ret_pct']}% 触发{r['count']}次 "
                             f"avg_max_dd={r['max_dd_pct']}%")
                if r.get('disc_ret_pct') is not None:
                    lines.append(f"     纪律收益={r['disc_ret_pct']}%(差{r.get('disc_impact')}%)"
                                 f" 止损触发{r.get('stop_rate')}% 止盈触发{r.get('tgt_rate')}%")
            if len(results) > 5:
                lines.append('')
                lines.append('其余策略：')
                for r in results[5:]:
                    lines.append(f"  {r['cn']:>12s} 胜率={r['win_rate']}% 触发{r['count']}次")
        else:
            lines.append('（无足够样本）')

        report = '\n'.join(lines)
        try:
            from notification_router import send
            send('archive', '📊 InStock 策略周度回测', report)
        except Exception as ne:
            print(f'[wf_weekly_backtest] 推送失败: {ne}\n{report}')

        _log_run(job, 'success',
                 error=f'pool={len(stocks)} strategies_evaluated={len(results)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_weekly_db_cleanup():
    """每周一凌晨：清理过期分析记录，VACUUM SQLite"""
    job = 'weekly_db_cleanup'
    started = datetime.now().isoformat()
    try:
        cleaned = 0
        notes = []
        # 清理 90 天前的持仓分析历史
        try:
            from portfolio_db import portfolio_db
            if hasattr(portfolio_db, 'delete_old_analysis'):
                cleaned += portfolio_db.delete_old_analysis(days=90) or 0
        except Exception as e:
            notes.append(f'cleanup_err={e}')
        # VACUUM:仅 SQLite 需要手动 VACUUM;PG 由 autovacuum 处理,
        # 且 VACUUM 不能在事务块内执行(db_compat 的 PG 连接非 autocommit → 必然报错)。
        if USE_POSTGRES:
            notes.append('vacuum_skipped=pg(autovacuum)')
        else:
            try:
                conn = db_connect(_SNAPSHOT_DB_PATH)
                conn.execute('VACUUM')
                conn.close()
            except Exception as e:
                notes.append(f'vacuum_err={e}')
        _log_run(job, 'success', error=f'cleaned_rows={cleaned}; ' + '; '.join(notes),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_kline_prefetch():
    """📥 盘后预热 K线缓存(开关 kline_prefetch,默认开)。
    全量预拉 持仓 + 监测 + 沪深300成分 的日线写入共享磁盘缓存(db/kline_cache),
    让 16:30 回测 / 因子IC / 晨报 / 持仓守卫 命中暖缓存(0ms),避免逐只冷拉外部源。
    实测主源每次返回的是已按当日复权因子重算的完整序列 → 全量拉即天然无复权漂移,
    故无需增量追加/锚点校验/周末特判:每个交易日盘后跑一次就是一次完整且正确的刷新。"""
    job = 'kline_prefetch'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        import datahub
        codes: dict = {}

        def _add(c):
            if c:
                s = str(c).strip().zfill(6)
                if s.isdigit() and len(s) == 6:
                    codes[s] = 1

        # 1) 持仓
        try:
            from portfolio_db import portfolio_db
            for s in portfolio_db.get_all_stocks() or []:
                _add(s.get('code') if isinstance(s, dict) else getattr(s, 'code', None))
        except Exception as e:
            print(f'[kline_prefetch] 持仓加载失败: {e}')
        # 2) 监测
        try:
            from monitor_db import monitor_db
            for s in monitor_db.get_monitored_stocks() or []:
                _add(s.get('symbol') if isinstance(s, dict) else getattr(s, 'symbol', None))
        except Exception as e:
            print(f'[kline_prefetch] 监测加载失败: {e}')
        # 3) 沪深300 成分
        try:
            from multi_factor_screener import get_index_universe
            for c in get_index_universe('000300') or []:
                _add(c)
        except Exception as e:
            print(f'[kline_prefetch] 指数成分加载失败: {e}')

        pool = list(codes)
        ok = bars = 0
        for c in pool:
            try:
                r = datahub.prefetch_kline(c)
                if r.get('bars'):
                    ok += 1
                    bars += r['bars']
            except Exception:
                pass
        msg = f'universe={len(pool)} warmed={ok} total_bars={bars}'
        print(f'[kline_prefetch] {msg}')
        # 顺便焐 collect_factors(内部含 full_valuation 同花顺 + pywencai 问财,都是慢源,TTL 1天)→
        # 盘中选股/取因子读暖缓存、0 调慢源,不再 09:45 逐只现调把线程池打满(雪崩的真主因)。
        # 盘后非高峰 + 全源熔断保护,慢源失败快速跳过。
        vwarm = 0
        try:
            from fundamental_scoring import collect_factors as _cf
            for c in pool:
                try:
                    f = _cf(c)
                    if f and any(v is not None for v in f.values()):
                        vwarm += 1
                except Exception:
                    pass
        except Exception as e:
            print(f'[kline_prefetch] 因子焐热不可用: {e}')
        print(f'[kline_prefetch] 因子(含估值/问财)焐热 {vwarm}/{len(pool)}')
        _log_run(job, 'success' if ok else 'error',
                 error=f'{msg} fv={vwarm}' if ok else f'no_kline_warmed; {msg}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def _push_report(title: str, body: str):
    """日常报告推送(默认 QQ webhook,主渠道全挂自动兜底邮件)。"""
    try:
        from notification_router import send
        send('report', title, body)
    except Exception:
        print(f'{title}\n{body}')


def _push_archive(title: str, body: str):
    """存档类长文推送(周报/AI评估等):邮件+QQ,邮件留档。"""
    try:
        from notification_router import send
        send('archive', title, body)
    except Exception:
        print(f'{title}\n{body}')


def _push_error(title: str, body: str):
    """告警推送(监控触发/任务失败/风险预警 → QQ 即时)"""
    try:
        from notification_router import send
        send('alert', title, body)
    except Exception:
        print(f'[ERROR PUSH FAILED] {title}\n{body}')


def _push_daily(title: str, body: str):
    """每日推送 — 与 _push_report 同路由(默认 QQ),保留别名兼容旧调用点。"""
    _push_report(title, body)


def _holdings_codes():
    """取持仓代码列表 [(code, name)],失败返回 []。"""
    try:
        from portfolio_db import portfolio_db
        out = []
        for s in (portfolio_db.get_all_stocks() or []):
            if isinstance(s, dict):
                c = s.get('code') or s.get('symbol')
                if c:
                    out.append((str(c), s.get('name', '')))
            elif s:
                out.append((str(s), ''))
        return out
    except Exception:
        return []


def _scan_holdings_with_snapshot():
    """持仓逐只扫描(零逐只K线接口):盘后指标快照 + 持仓成本 + 批量实时行情。

    晨报AI(09:00)/早盘持仓(09:50)/尾盘持仓(14:30)共用。
    返回 [{'code','name','price','change','pnl','sell_score','sell_reasons',
           'buy_signal','buy_reason'}],失败返回 []。
    卖出风险分: 破MA60(+2)/破MA20(+1)/VaR95>5%(+1)/年回撤>40%(+1)/浮亏>10%(+1)/
               缠论卖点(+1)/盘中大跌>5%(+1)
    买点: 缠论 一买/二买/三买/底背驰
    """
    try:
        from portfolio_db import portfolio_db as _pdb
        holds = [h for h in (_pdb.get_all_stocks() or []) if isinstance(h, dict) and h.get('code')]
    except Exception as e:
        print(f'[_scan_holdings] 读取持仓失败: {type(e).__name__}: {str(e)[:80]}', flush=True)
        return []
    codes = [str(h['code']) for h in holds]
    print(f'[_scan_holdings] 持仓 {len(codes)} 只, 开始批量拉行情', flush=True)
    quotes = {}
    _qt0 = time.time()
    try:
        for i in range(0, len(codes), 20):
            quotes.update(datahub.quotes(codes[i:i + 20]) or {})
    except Exception as e:
        print(f'[_scan_holdings] 批量行情失败: {type(e).__name__}', flush=True)
    print(f'[_scan_holdings] 行情拿到 {len(quotes)} 只, 耗时 {time.time()-_qt0:.1f}s, 开始逐只算指标', flush=True)

    def _snapf(snap, *keys):
        for k in keys:
            v = snap.get(k)
            if v not in (None, ''):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    scans = []
    for h in holds:
        try:
            code = str(h['code'])
            snap = get_indicator_snapshot(code) or {}
            nosnap = not snap  # 冷启动/快照断档:技术指标缺失,只剩行情维度
            q = quotes.get(code) or {}
            price = float(q.get('price') or 0)
            change = float(q.get('change_pct') or 0)
            cost = float(h.get('cost_price') or h.get('cost') or 0)
            pnl = round((price - cost) / cost * 100, 1) if (price > 0 and cost > 0) else None
            ma20, ma60 = _snapf(snap, 'ma20', 'MA20'), _snapf(snap, 'ma60', 'MA60')
            var95, mdd = _snapf(snap, 'var95'), _snapf(snap, 'max_drawdown')
            chan = str(snap.get('chan_signal') or '')
            score, reasons = 0, []
            if price > 0 and ma60 and price < ma60 * 0.98:
                score += 2; reasons.append(f'破MA60({ma60:.2f})')
            elif price > 0 and ma20 and price < ma20:
                score += 1; reasons.append(f'破MA20({ma20:.2f})')
            if var95 is not None and var95 > 0.05:
                score += 1; reasons.append(f'VaR95 {var95 * 100:.1f}%')
            if mdd is not None and mdd < -0.40:
                score += 1; reasons.append(f'年回撤{mdd * 100:.0f}%')
            if pnl is not None and pnl <= -10:
                score += 1; reasons.append(f'浮亏{pnl}%')
            if '卖' in chan or '顶背驰' in chan:
                score += 1; reasons.append(f'缠论{chan}')
            if change <= -5:
                score += 1; reasons.append(f'盘中大跌{change:.1f}%')
            buy_sig = any(k in chan for k in ('一买', '二买', '三买', '底背驰'))
            qty = 0.0
            try:
                qty = float(h.get('quantity') or h.get('shares') or 0)
            except (TypeError, ValueError):
                pass
            scans.append({'code': code, 'name': q.get('name') or h.get('name', ''),
                          'price': price or None, 'change': change, 'pnl': pnl,
                          'mv': round(price * qty, 0) if (price > 0 and qty > 0) else None,
                          'var95': var95, 'nosnap': nosnap,
                          'sell_score': score, 'sell_reasons': reasons,
                          'buy_signal': buy_sig, 'buy_reason': f'缠论{chan}' if buy_sig else ''})
        except Exception:
            continue
    return scans


# =====================================================================
# 🆕 整合后的新任务（原始任务函数保留不动，仅此为新的调度入口）
# =====================================================================

def task_main_force_prefetch():
    """🏦 盘前预取主力选股(问财全市场"主力资金净流入排名")→ 写当日缓存。
    09:45 unified_selection 的"主力资金"策略读这份缓存,不在选股高峰现调问财
    (问财熔断/卡死时主力选股会退化成"按市值选股")。非交易日跳过。失败不告警:09:45 自动 fallback 现调。"""
    job = 'main_force_prefetch'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        from main_force_selector import MainForceStockSelector
        mf = MainForceStockSelector()
        ok, df, msg = mf.get_main_force_stocks_cached(days_ago=5, use_cache=False)  # 强制现取并回写
        n = len(df) if (ok and df is not None and hasattr(df, 'empty') and not df.empty) else 0
        print(f'[main_force_prefetch] {"✅" if n else "⚠️"} 预取主力选股 {n} 只 ({msg})', flush=True)
        _log_run(job, 'success' if n else 'error',
                 error=None if n else f'prefetch_empty: {msg}',
                 started_at=started, finished_at=datetime.now().isoformat(), notify=False)
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat(), notify=False)


def task_unified_selection():
    """🆕 整合选股：4大策略 + InStock10 + 多因子 + 个人过滤 →  TOP 15"""
    job = 'unified_selection'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        # candidates: code -> {'score': float, 'src': [来源标签]}(2026-06-12 加来源追踪,推送可解释)
        candidates = {}

        def _add(code, pts, src):
            c = candidates.setdefault(code, {'score': 0.0, 'src': []})
            c['score'] += pts
            if src and src not in c['src']:
                c['src'].append(src)

        # 1. 5大策略扫描(问财/dataapi) → 初选池
        strategy_scan = _run_strategy_scans()
        for sname, (ok, df, msg) in strategy_scan.get('results', {}).items():
            if ok and df is not None:
                for _, row in df.iterrows():
                    code = next((row[c] for c in ['股票代码', 'code', 'symbol'] if c in row.index), None)
                    if code:
                        _add(code, 1.0, sname)

        # 2. InStock 13 策略扫描（对持仓+候选池批量跑,用基因组进化后的最优参数+组合新策略,按横截面评分加权）
        strategy_weights = {}  # 提前定义:step2 整体失败时,后面"基因组热度摘要"不至于 NameError
        try:
            from instock_strategy_runner import run_batch
            stock_list = [(c, '') for c in list(candidates.keys())[:30]]
            instock_results = run_batch(stock_list, evolved=True)

            # 取策略基因组最新横截面评分，作为命中权重
            try:
                from analysis.strategy_genome import get_strategy_intelligence
                intel = get_strategy_intelligence(days=60)
                for m in intel.get('market', []):
                    sid = m.get('strategy_id', '')
                    sc = m.get('score', 50) or 50
                    strategy_weights[sid] = sc / 100.0  # 0~1 权重
            except Exception:
                pass

            for r in instock_results:
                sym = r.get('symbol', '')
                matches = r.get('matched', [])
                if matches:
                    # 修复:matched 是 [{'id','cn','category'}],原来拿整个 dict 当权重表的 key
                    # → TypeError 被外层吞掉,InStock 加权从来没生效过
                    for m in matches:
                        if not isinstance(m, dict):
                            continue
                        mid = m.get('id', '')
                        base_id = mid.split(':')[0] if mid.startswith('composed:') else mid
                        _add(sym, strategy_weights.get(base_id, 1.0), m.get('cn', mid))
        except Exception:
            pass

        # 3. 多因子打分:5 大策略 / InStock / 多因子 平权各 +1, 让 TOP 由"被多个 source 命中"
        # 主导, 而不是被多因子单一来源垄断(2026-06-16 调权:此前多因子 +2 比 5 大策略 +1 高一倍,
        # 加上 InStock 基因组初期还没积累 → TOP 15 全是多因子, 失去多策略综合的意义)。
        try:
            from multi_factor_screener import screen_index_cached
            mf_result = screen_index_cached(index_code='000300', n=25, add_sector_leaders=True,
                                            workers=1, force=False, ttl=3600)
            for item in mf_result.get('top', []):
                sym = item.get('symbol', '')
                if sym:
                    _add(sym, 1.0, '多因子')
        except Exception:
            pass

        # 排序 TOP 15:总分降序, 同分按"命中 source 数"二级排序(被多个策略命中的优先);
        # **强制 source 多样性**:每个 source 最多 _MAX_PER_SOURCE 只, 防单一来源垄断。
        # 配额满了就跳过, 取下一只(直到 15 个或候选耗尽)。
        _MAX_PER_SOURCE = int(os.environ.get('UNIFIED_SELECTION_MAX_PER_SOURCE', '4'))
        sorted_all = sorted(candidates.items(),
                            key=lambda x: (-x[1]['score'], -len(x[1].get('src', []))))
        source_count: Dict[str, int] = {}
        top_list: List[str] = []
        skipped_by_quota: List[str] = []
        for code, info in sorted_all:
            if len(top_list) >= 15:
                break
            srcs = info.get('src', []) or ['-']
            # 这只所有来源都没达到配额上限, 才入选(任一来源已满 → 跳过)
            if any(source_count.get(s, 0) >= _MAX_PER_SOURCE for s in srcs):
                skipped_by_quota.append(code)
                continue
            top_list.append(code)
            for s in srcs:
                source_count[s] = source_count.get(s, 0) + 1
        # 配额制可能凑不满 15 只 → 用之前被跳过的高分股按原排序补齐
        if len(top_list) < 15 and skipped_by_quota:
            for code in skipped_by_quota:
                if code in top_list:
                    continue
                top_list.append(code)
                if len(top_list) >= 15:
                    break
        held_codes = {c for c, _ in _holdings_codes()}

        # 批量拉行情（一次性比15次单独调快得多）
        quotes_cache = {}
        try:
            raw = datahub.quotes(top_list)
            if isinstance(raw, dict):
                quotes_cache = raw
        except Exception:
            pass
        # 名字:独立于行情解析(开盘抢数据时行情源可能抽风,但中文名走持久缓存,
        # 不至于退化成 "600595 600595" 代码当名字)
        try:
            name_map = datahub.stock_names(top_list)
        except Exception:
            name_map = {}

        # 红蓝对抗整合(2026-06-24):上午不再单独推一条,TOP10 的多空对抗结论直接并进本表
        # (妙想第二意见 10:30 仍独立)。best-effort:LLM 挂了红蓝列留空,不影响选股表;
        # 决策信号在此写(record_signals=True),原 selection_debate 独立任务因此跳过不重复。
        # ⚡ 整体超时护栏(2026-06-25 修):红蓝是 10 只 LLM,外网/LLM 挂时会拖很久 → 套 6min 硬超时,
        # 超了放弃红蓝(表格红蓝列留空),**保住选股核心先推出去**,不让 unified 被红蓝拖到 1800s 超时崩。
        debate_map = {}
        try:
            from selection_debate import run_selection_debate
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=1) as _dex:
                _dres = _dex.submit(run_selection_debate, top_list[:10], 10, True).result(timeout=360)
            for _it in (_dres.get('items') or []):
                debate_map[str(_it.get('code'))] = _it
        except Exception as _de:
            print(f'[unified_selection] 红蓝对抗放弃(不影响选股): {type(_de).__name__}: {str(_de)[:60]}')

        # 输出（Markdown 表格,含红蓝/来源列;💼=已持仓）
        body = f'## 🎯 综合选股 TOP {len(top_list)}\n'
        body += f'📅 {datetime.now().strftime("%Y-%m-%d %H:%M")}\n\n'
        body += '| # | 代码 名称 | 价格 | 涨跌 | PE | 分 | 红蓝 | 来源 |\n'
        body += '|:-:|:---------|:---:|:---:|:---:|:-:|:--:|:----|\n'
        _debate_tag = {'买入': '🔴可买', '谨慎': '🟡观望', '否决': '🟢避开'}
        for i, code in enumerate(top_list, 1):
            cinfo = candidates.get(code) or {'score': 0, 'src': []}
            score = round(cinfo['score'], 1)
            src_s = '/'.join(cinfo['src'])[:24] or '-'
            # 行情优先用批量缓存，不再逐只调 get_stock_info（省 15 次网络请求）
            q = (quotes_cache.get(code) or quotes_cache.get(str(code)[-6:]) or {})
            name = q.get('name') or name_map.get(code) or code
            price = q.get('price', '?')
            pct = q.get('change_pct', '')
            pe = q.get('pe_ttm', '')
            # 涨跌 emoji
            try:
                fpct = float(pct) if pct and pct != 'N/A' and pct != '?' else None
            except (ValueError, TypeError):
                fpct = None
            # A 股惯例:红涨绿跌
            arrow = '🔴' if fpct and fpct > 0 else ('🟢' if fpct and fpct < 0 else '⚪')
            held = '💼' if code in held_codes else ''

            pct_s = f'{fpct:+.2f}%' if fpct is not None else '-'
            pe_s = f'{float(pe):.1f}' if pe and pe != 'N/A' and pe != '?' and float(pe) > 0 else '-'
            price_s = f'{price}' if price and price != 'N/A' and price != '?' else '-'
            _dv = debate_map.get(code)
            debate_s = _debate_tag.get(_dv['verdict'], '') if _dv else '-'

            body += f'| {i} | {arrow}{held} {code} {name} | ¥{price_s} | {pct_s} | {pe_s} | {score} | {debate_s} | {src_s} |\n'

        # 红蓝对抗速览(结论先行):被否决的直接点名"避开"
        if debate_map:
            _rej = [debate_map[c] for c in top_list[:10]
                    if debate_map.get(c, {}).get('verdict') == '否决']
            _pass_n = sum(1 for v in debate_map.values() if v.get('verdict') == '买入')
            body += (f'\n\n⚔️ 红蓝对抗:{_pass_n} 只可买、{len(_rej)} 只建议避开')
            if _rej:
                body += '\n🟢 避开:' + '、'.join((r.get('name') or r['code']) for r in _rej)

        # ── 红蓝对抗门控(2026-06-26):被「否决」的票剔出推荐池/盘后扫描快照,不再当推荐追踪胜率 ──
        # 此前否决票照样入池污染胜率闭环(ai_eval_weekly)。表格仍显示🟢避开(可见性不丢),只停止入池。
        # 只拦「否决」,不误伤「谨慎/买入」。开关 SELECTION_DEBATE_GATE(默认开;设 false/0/no/off 关闭)。
        if debate_map and os.getenv('SELECTION_DEBATE_GATE', 'true').lower() not in ('false', '0', 'no', 'off'):
            _vetoed = [c for c in top_list if debate_map.get(c, {}).get('verdict') == '否决']
            if _vetoed:
                top_list = [c for c in top_list if c not in set(_vetoed)]
                body += f'\n🛡️ 门控:已剔除 {len(_vetoed)} 只(不入推荐池/不再追踪胜率)'
                print(f'[unified_selection] 红蓝门控剔除 {len(_vetoed)} 只: {"、".join(_vetoed)}')

        # 附：策略基因组热度摘要
        if strategy_weights:
            ranked_weights = sorted(strategy_weights.items(), key=lambda x: -x[1])[:5]
            w_lines = ' · '.join(f'{k} x{w:.2f}' for k, w in ranked_weights)
            body += f'\n\n📊 策略评分加权（高分命中权重高）：\n{w_lines}'
            body += '\n💡 跑几天后策略会自进化，选股自动向高效策略倾斜'

        # 缓存选股结果供 mx_selection_review 读取（挪到 _log_run 前，防 _log_run 异常吞掉）
        try:
            save_indicator_snapshot('_last_selection', {'picks': top_list})
        except Exception:
            pass

        # ── 选股战绩闭环(2026-06-12):TOP10 入推荐池记录(不启监控,零成本) ──
        # ai_eval_weekly 每周按 source 算真实胜率 → _source_feedback 反哺门槛。
        # 此前 TOP15 发完即消失,没人知道综合选股的真实命中率。开关 wf_selection_to_rec(默认开)。
        try:
            from automation_config import is_enabled
            if is_enabled('wf_selection_to_rec'):
                from ai_recommendation_monitor import save_recommendation
                rec_n = 0
                for code in top_list[:10]:
                    if code in held_codes:
                        continue  # 已持仓的不算"新推荐"
                    q = (quotes_cache.get(code) or quotes_cache.get(str(code)[-6:]) or {})
                    cinfo = candidates.get(code) or {}
                    try:
                        rid = save_recommendation(
                            symbol=str(code), name=q.get('name') or name_map.get(code, ''),
                            source='unified_selection', rating='candidate',
                            confidence='中',
                            ref_price=_safe_float(q.get('price')),
                            reason=('综合选股 分' + str(round(cinfo.get('score', 0), 1))
                                    + ' 来源:' + '/'.join(cinfo.get('src', []))[:120]),
                        )
                        if rid:
                            rec_n += 1
                    except Exception:
                        continue
                if rec_n:
                    print(f'[unified_selection] {rec_n} 只入推荐池追踪(source=unified_selection)')
        except Exception as e:
            print(f'[unified_selection] 战绩闭环失败: {e}')

        # 多源回喂:附本 source 近90天真实战绩(Y3.1 已让 candidate 能算战绩),闭合"选股→战绩→可见→校准"环
        try:
            _fb_us = _source_feedback('unified_selection')
            if _fb_us.get('text') and '无历史战绩' not in _fb_us['text']:
                body += f'\n\n📈 综合选股{_fb_us["text"]}'
        except Exception:
            pass

        _push_daily('🎯 综合选股 TOP 15', body)
        _log_run(job, 'success', error=f'picks={len(top_list)}',
                 started_at=started, finished_at=datetime.now().isoformat())

    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())

    # ── 个人口味候选池（原 wf_daily_candidate_pool,开关控制,默认开）──
    try:
        _daily_candidate_pool()
    except Exception as e:
        print(f'[unified_selection] 候选池子任务失败: {e}')


def task_morning_portfolio():
    """🆕 早盘持仓分析（接住原晨报"持仓买卖提示":多因子风险分+浮盈,9:50 开盘后实时价比 9:00 盘前快照更准）

    数据全部现成:盘后指标快照 + 持仓成本 + 一次批量实时行情(_scan_holdings_with_snapshot)。
    """
    job = 'morning_portfolio'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        scans = _scan_holdings_with_snapshot()
        if not scans:
            _log_run(job, 'success', error='no holdings', started_at=started,
                     finished_at=datetime.now().isoformat())
            return

        sell_list = sorted([s for s in scans if s['sell_score'] > 0],
                           key=lambda x: x['sell_score'], reverse=True)[:5]
        buy_list = [s for s in scans if s['buy_signal']][:8]
        # 盘中异动(开盘40分钟):涨>3% 或 跌>3%
        movers = sorted([s for s in scans if abs(s.get('change') or 0) >= 3],
                        key=lambda x: x['change'], reverse=True)[:6]

        lines = [f'## ☀️ 早盘持仓分析 — {datetime.now().strftime("%Y-%m-%d %H:%M")}', '']

        # 大盘速览(轻量,新浪源)
        try:
            import briefing as _brief
            _mkt = _brief._market()
            if _mkt.get('indices'):
                lines.append('【大盘】' + '  '.join(f"{x['name']}{x['v']}" for x in _mkt['indices']))
            if _mkt.get('sector_top'):
                lines.append('强势板块: ' + '、'.join(f"{s['板块']}{s['涨跌幅']}%" for s in _mkt['sector_top']))
            lines.append('')
        except Exception:
            pass

        lines.append('### 🔴 该减/清的(结论先行,按紧迫度)')
        # 术语翻人话:用户看"跌破60日线"比"破MA60(12.3)"懂
        _RTERM = [('破MA60', '跌破60日线·中期破位'), ('破MA20', '跌破20日线·短期走弱'),
                  ('VaR95', '短期波动风险偏高'), ('年回撤', '一年内回撤大')]
        if sell_list:
            for s in sell_list:
                sc = s['sell_score']
                # 风险分 → 动作+减仓比例(破MA60=+2,故 ≥4 多为破位+多重风险)
                act = '🔴 清仓/大幅减' if sc >= 4 else ('🟡 减一半' if sc >= 2 else '⚪ 先关注')
                pnl_s = f"，浮盈{s['pnl']}%" if s['pnl'] is not None else ''
                price_s = f"¥{s['price']}" if s['price'] else ''
                why = '、'.join(next((h for k, h in _RTERM if r.startswith(k)), r)
                               for r in s['sell_reasons'])
                lines.append(f"  • {act} ｜ {s['name']} {s['code']} {price_s}{pnl_s}")
                lines.append(f"      为什么:{why}")
        else:
            lines.append('  （暂无预警）')

        lines.append('')
        lines.append('### 🟢 持仓出现买点')
        if buy_list:
            for s in buy_list:
                price_s = f"¥{s['price']}" if s['price'] else ''
                lines.append(f"  • {s['name']} {s['code']} {price_s} — {s['buy_reason']}")
        else:
            lines.append('  （暂无信号）')

        if movers:
            lines.append('')
            lines.append('### ⚡ 盘中异动(±3%)')
            for s in movers:
                lines.append(f"  • {s['name']} {s['code']} {s['change']:+.1f}%"
                             + (f"  浮盈{s['pnl']}%" if s['pnl'] is not None else ''))

        # 仓位一行(超限/集中度提示)
        try:
            from analysis.position_sizer import analyze as _ps_analyze, format_brief as _ps_brief
            brief = _ps_brief(_ps_analyze(scans))
            if brief:
                lines.append('')
                lines.append(brief)
        except Exception:
            pass

        nosnap_n = sum(1 for s in scans if s.get('nosnap'))
        if nosnap_n:
            lines.append('')
            lines.append(f'⚠️ {nosnap_n}/{len(scans)} 只无盘后指标快照(等 15:45 快照任务跑过后完整)')

        _push_daily('☀️ 早盘持仓分析', '\n'.join(lines))

        # 🎯 挑「今日重点盯盘候选」(持仓多→聚焦):风险分>0 / 有买点 / 盘中异动±3%。
        # 存快照供 11:20 午间盯盘只看这批(不再全持仓逐只),与"持仓瘦身"理念一致。
        try:
            cands = []
            for s in scans:
                hot = s['sell_score'] > 0 or s['buy_signal'] or abs(s.get('change') or 0) >= 3
                if not hot:
                    continue
                if s['sell_score'] > 0:
                    tag = '⚠️ ' + '、'.join(s['sell_reasons'][:2])
                elif s['buy_signal']:
                    tag = '🟢 ' + (s.get('buy_reason') or '买点')
                else:
                    tag = f"⚡ 异动{s.get('change'):+.1f}%"
                pri = s['sell_score'] * 2 + (1 if s['buy_signal'] else 0) + (1 if abs(s.get('change') or 0) >= 3 else 0)
                cands.append({'code': s['code'], 'name': s['name'], 'pri': pri, 'tag': tag,
                              'sell_score': s['sell_score'], 'mprice': s.get('price') or 0})
            cands.sort(key=lambda x: x['pri'], reverse=True)
            save_indicator_snapshot('focus_candidates',
                                    {'date': datetime.now().strftime('%Y-%m-%d'), 'picks': cands[:15]})
        except Exception as _e:
            print(f'[morning_portfolio] 候选挑选失败: {_e}', flush=True)

        _log_run(job, 'success',
                 error=f'scanned={len(scans)} sell={len(sell_list)} buy={len(buy_list)} movers={len(movers)}',
                 started_at=started, finished_at=datetime.now().isoformat())

    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_afternoon_portfolio():
    """🧹 尾盘持仓总结(14:40)—— 整合原「尾盘持仓分析 + 持仓AI体检 + 清仓助手」三条重叠推送为一条。
    一次取数 + 一次 LLM → 整体瘦身策略 + 逐只**融合**动作(每只一个结论,不再三处口径打架)+ 尾盘机会;
    清仓/减仓写 decision_signal(source_type='eod_review')。详见 `portfolio/eod_review.py`。
    末尾仍跑 止盈阶梯/破位减仓信号 子任务(alert 渠道,按需,保留不并)。"""
    job = 'afternoon_portfolio'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        import os as _os6
        target = int(_os6.getenv('EXIT_TARGET_POSITIONS', '20'))
        from eod_review import run_eod_review
        res = run_eod_review(target_positions=target, record_signals=True)
        if res.get('ok') and res.get('text'):
            _push_daily('🧹 尾盘持仓总结', res['text'])
        _log_run(job, 'success', error=res.get('summary'),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())

    # ── 止盈阶梯/破位减仓信号(alert,按需,保留;不并入尾盘总结)──
    try:
        _position_profit_check()
    except Exception as e:
        print(f'[afternoon_portfolio] 减仓信号子任务失败: {e}')

    # ── E: 盘中急跌兜底(14:30 尾盘段,每股每日去重)。覆盖点 10:30/11:20/14:30 三次,不重复告警 ──
    try:
        _intraday_plunge_check()
    except Exception as e:
        print(f'[afternoon_portfolio] 急跌监控子任务失败: {e}')


def task_noon_portfolio():
    """🕦 午间持仓盯盘(11:20)—— 只看早盘(09:50)挑出的「今日重点候选」, 不再全持仓逐只。

    持仓多(80只)全程逐只盯既费算力又抓不住重点 → 早盘 morning_portfolio 按 风险分/买点/异动
    挑出 top15 存 focus_candidates 快照, 午间只跟这批。一组批量行情(零逐只K线)看候选当前价/异动,
    推简报; 尾接持仓急跌兜底(原挂 stock_monitor_check 每30分, 该任务退役后移到此单点)。"""
    job = 'noon_portfolio'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        snap = get_indicator_snapshot('focus_candidates') or {}
        picks = (snap.get('picks') or []) if snap.get('date') == today else []
        if not picks:
            _log_run(job, 'success', error='no focus candidates (早盘未挑/无持仓)',
                     started_at=started, finished_at=datetime.now().isoformat())
        else:
            codes = [p['code'] for p in picks]
            quotes = {}
            try:
                for i in range(0, len(codes), 20):
                    quotes.update(datahub.quotes(codes[i:i + 20]) or {})
            except Exception:
                pass
            lines = [f'## 🕦 午间重点盯盘 — {datetime.now().strftime("%Y-%m-%d %H:%M")}',
                     f'(早盘挑出 {len(picks)} 只重点, 午间只跟这批)', '']
            for p in picks:
                q = quotes.get(p['code']) or {}
                try:
                    price = float(q.get('price') or 0)
                    chg = float(q.get('change_pct') or 0)
                except (TypeError, ValueError):
                    price, chg = 0, 0
                mark = '🔴' if chg <= -3 else ('🟢' if chg >= 3 else '·')
                price_s = f'¥{price}' if price else ''
                lines.append(f"  {mark} {p['name']} {p['code']} {price_s} {chg:+.1f}%  {p.get('tag', '')}")
            _push_daily('🕦 午间重点盯盘', '\n'.join(lines))
        _log_run(job, 'success', error=f'candidates={len(picks)}',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())

    # ── 持仓急跌兜底(原挂 stock_monitor_check 每30分, 退役后移到此单点)──
    try:
        _intraday_plunge_check()
    except Exception as e:
        print(f'[noon_portfolio] 急跌监控子任务失败: {e}', flush=True)


def task_portfolio_health_ai():
    """🧠 持仓 AI 体检官(14:35 尾盘):融合每只持仓的多维规则信号 → 单股 持有/减仓/清仓 动作 + 理由。
    动作写 decision_signal(source_type='portfolio_health')→ 16:10 自动方向后验,形成可验证胜率环。
    开关 portfolio_health_ai(默认开;只对风险/浮亏子集做,token 可控)。"""
    job = 'portfolio_health_ai'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        scans = _scan_holdings_with_snapshot()
        if not scans:
            _log_run(job, 'success', error='no_holdings',
                     started_at=started, finished_at=datetime.now().isoformat())
            return
        from portfolio_health_ai import run_health_check
        res = run_health_check(scans, max_stocks=15, record_signals=True)
        if res.get('ok') and res.get('text'):
            try:
                from notification_router import send
                send('report', '🧠 持仓 AI 体检', res['text'])
            except Exception as ne:
                print(f'[portfolio_health_ai] 推送失败: {ne}')
        _log_run(job, 'success', error=res.get('summary'),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_portfolio_stress_ai():
    """🛡️ 组合压力情景叙事官(周日 16:00):跑全 8 宏观情景压力 + 集中度 → AI 风险预案
    (最脆弱情景/风险担当持仓/具体减仓对冲建议)。开关 portfolio_stress_ai(默认开)。周末照跑(静态分析)。"""
    job = 'portfolio_stress_ai'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    started = datetime.now().isoformat()
    try:
        from portfolio_stress_ai import run_stress_narrative
        res = run_stress_narrative(include_funds=True)
        if res.get('ok') and res.get('text'):
            try:
                from notification_router import send
                send('report', '🛡️ 组合压力预案', res['text'])
            except Exception as ne:
                print(f'[portfolio_stress_ai] 推送失败: {ne}')
        _log_run(job, 'success', error=res.get('summary'),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def _holdings_plus_selection() -> list:
    """持仓 + 当日综合选股 代码并集(供事件/公告扫描)。"""
    codes = []
    try:
        from portfolio_db import portfolio_db
        codes += [str(s.get('code')) for s in (portfolio_db.get_all_stocks() or [])
                  if s.get('code') and float(s.get('quantity') or s.get('shares') or 0) > 0]
    except Exception:
        pass
    codes += _last_selection_picks()
    return list(dict.fromkeys([c for c in codes if c]))


def task_announcement_scan():
    """⚠️ 盘后风险预警(16:02 三合一,2026-06-24):解禁雷达 + 公告分级 + 研报解读 合并成一条推送
    (原 15:48 解禁 / 16:05 研报 已并入,不再单独推 → 盘后从 3 条变 1 条;三者各自仍写 decision_signal,
    16:10 后验不变)。有解禁/公告利空 → 走 alert(结论先行、优先看);否则中性内容走 report。
    开关 announcement_scan(默认开)。"""
    job = 'announcement_scan'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        codes = _holdings_plus_selection()
        hold_codes = []
        try:
            from portfolio_db import portfolio_db
            hold_codes = [str(s.get('code')) for s in (portfolio_db.get_all_stocks() or [])
                          if s.get('code') and float(s.get('quantity') or s.get('shares') or 0) > 0]
        except Exception:
            pass
        if not codes:
            _log_run(job, 'success', error='no_codes', started_at=started,
                     finished_at=datetime.now().isoformat())
            return
        risk_parts, info_parts, summ = [], [], []
        # ① 解禁雷达(仅持仓,未来60天)
        try:
            if hold_codes:
                from lockup_radar import run_lockup_radar
                lr = run_lockup_radar(hold_codes, forward_days=60, min_ratio=0.03, record_signals=True)
                if lr.get('items'):
                    risk_parts.append('【⏳ 解禁风险】\n' + (lr.get('text') or ''))
                    summ.append(f"解禁{len(lr['items'])}")
        except Exception as e:
            print(f'[eod_risk] 解禁失败: {type(e).__name__}: {str(e)[:60]}')
        # ② 公告分级(持仓+选股,近5天)
        try:
            from announcement_scan import run_announcement_scan
            an = run_announcement_scan(codes, days=5, record_signals=True)
            if an.get('alerts'):
                bad = '\n'.join(f"🟢{a['name']} {a['code']}[{a['type']}] {a['summary']}" for a in an['alerts'])
                risk_parts.append('【📢 公告利空】\n' + bad)
                summ.append(f"公告利空{len(an['alerts'])}")
            elif an.get('ok') and an.get('text'):
                info_parts.append('【📢 公告事件】\n' + an['text'])
        except Exception as e:
            print(f'[eod_risk] 公告失败: {type(e).__name__}: {str(e)[:60]}')
        # ③ 研报解读(持仓+选股,近10天)
        try:
            from research_digest import run_research_digest
            rd = run_research_digest(codes, days=10, max_llm=24, record_signals=True)
            if rd.get('ok') and rd.get('text'):
                info_parts.append('【📑 研报解读】\n' + rd['text'])
                summ.append('研报')
        except Exception as e:
            print(f'[eod_risk] 研报失败: {type(e).__name__}: {str(e)[:60]}')
        # 合并推送:结论先行——有风险走 alert 一条,否则中性走 report 一条
        try:
            from notification_router import send
            if risk_parts:
                body = '⚠️ 盘后风险预警 — 解禁/公告利空优先看\n\n' + '\n\n'.join(risk_parts)
                if info_parts:
                    body += '\n\n——以下中性参考——\n\n' + '\n\n'.join(info_parts)
                send('alert', '⚠️ 盘后风险预警', body)
            elif info_parts:
                send('report', '📑 盘后盘点(公告/研报)', '\n\n'.join(info_parts))
        except Exception as ne:
            print(f'[eod_risk] 推送失败: {ne}')
        _log_run(job, 'success', error='|'.join(summ) or 'no_risk', started_at=started,
                 finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_exit_advice():
    """🧹 清仓决策助手(14:40 尾盘):对全部持仓打"清仓紧迫分"排序 + 过度分散瘦身建议 + AI 整体策略。
    清仓/减仓结论写 decision_signal(source_type='exit_advice')→ 16:10 方向后验。开关 exit_advice(默认开)。"""
    job = 'exit_advice'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        import os as _os5
        target = int(_os5.getenv('EXIT_TARGET_POSITIONS', '20'))
        from exit_advisor import run_exit_advice
        res = run_exit_advice(target_positions=target, record_signals=True)
        if res.get('ok') and res.get('text'):
            try:
                from notification_router import send
                send('report', '🧹 清仓决策助手', res['text'])
            except Exception as ne:
                print(f'[exit_advice] 推送失败: {ne}')
        _log_run(job, 'success', error=res.get('summary'),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def task_mx_selection_review():
    """🆕 选股结果过妙想——对 unified_selection TOP 逐个过妙想诊断"""
    job = 'mx_selection_review'
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        # 读缓存的选股结果(统一走 _last_selection_picks,修复原 snap.get('indicators') 死分支)
        top_list = _last_selection_picks()

        if not top_list:
            _log_run(job, 'success', error='no selection cache', started_at=started,
                     finished_at=datetime.now().isoformat())
            return

        from analysis.miaoxiang import stock_diagnosis
        try:
            name_map = datahub.stock_names(top_list[:10])
        except Exception:
            name_map = {}

        agree, watch, avoid, detail = [], [], [], []
        for code in top_list[:10]:
            nm = name_map.get(code) or code
            try:
                result = stock_diagnosis(code)
                low = str(result).lower()
                if 'buy' in low:
                    verdict, bucket = '✅ 买入', agree
                elif 'sell' in low:
                    verdict, bucket = '❌ 规避', avoid
                else:
                    verdict, bucket = '⚠️ 观望', watch
                bucket.append(f'{nm} {code}')
                detail.append(f'{nm} {code}: {verdict}')
            except Exception:
                detail.append(f'{nm} {code}: ⚠️ 诊断失败')

        # D: 只在"妙想与综合选股分歧"时才推 —— 妙想对 unified 选中的票判❌规避才算实质分歧;
        # 全是买入/观望则不打扰(意见仍记 job_runs 可查)。env MX_REVIEW_ALWAYS_PUSH=true 恢复每日推。
        import os as _osmx
        always = _osmx.getenv('MX_REVIEW_ALWAYS_PUSH', 'false').lower() in ('1', 'true', 'yes', 'on')
        if avoid or always:
            head = (f'## 🔍 妙想第二意见 — {datetime.now().strftime("%Y-%m-%d %H:%M")}\n'
                    '_东财妙想 AI 对今日入选股的独立看法,仅作交叉验证_\n')
            if avoid:
                head += f'\n⚠️ 与综合选股分歧({len(avoid)} 只妙想判规避):' + '、'.join(avoid) + '\n'
            _push_daily('🔍 妙想第二意见', head + '\n' + '\n'.join(detail))
        _log_run(job, 'success',
                 error=f'买入{len(agree)}/观望{len(watch)}/规避{len(avoid)}'
                       + ('(分歧已推)' if avoid else '(一致,未推)'),
                 started_at=started, finished_at=datetime.now().isoformat())

    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())

    # ── E: 盘中急跌兜底(10:30 段,每股每日去重)。覆盖点 10:30/11:20/14:30 三次,不重复告警 ──
    try:
        _intraday_plunge_check()
    except Exception as e:
        print(f'[mx_selection_review] 急跌监控子任务失败: {e}')


def task_mx_daily_analysis():
    """收盘后妙想复盘: run_daily_wrap 一站式完成(收集数据→调妙想→格式化)→推送"""
    job = 'mx_daily_analysis'
    if _skip_if_not_trading(job):   # 收盘复盘只在交易日有意义:非交易日跳过,免白调妙想AI/推无效复盘
        return
    started = datetime.now().isoformat()
    try:
        from jobs.mx_advisor import run_daily_wrap

        report = run_daily_wrap()
        if not report:
            _log_run(job, 'success', error='no data', started_at=started,
                     finished_at=datetime.now().isoformat())
            return

        from notification_router import send
        send('report', '🌙 妙想收盘复盘', report)
        _log_run(job, 'success', error='ok',
                 started_at=started, finished_at=datetime.now().isoformat())

    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_mx_weekend_outlook():
    """🔮 周末妙想研判(周日):本周复盘 + 下周展望 + 热点题材 + 重点行业。
    周末无盘、妙想空着 → 充分利用做前瞻研究(无交易日守卫:周日本就非交易日,照跑)。"""
    job = 'mx_weekend_outlook'
    started = datetime.now().isoformat()
    try:
        from analysis.miaoxiang import finance_ask, hotspot
        # 用最灵活的 ask(七合一)承载开放式展望,hotspot 用文档式问法(特化 skill 对问法挑剔)。各自 try 隔离。
        segments = [
            ('📅 本周复盘 · 下周展望', finance_ask,
             '回顾本周A股市场整体表现与资金动向(指数涨跌/风格切换/北向资金/市场情绪),并展望下周行情节奏与操作策略'),
            ('🔥 热点板块', hotspot,
             '本周A股有哪些热点板块'),
            ('📌 下周关注', finance_ask,
             '下周A股有哪些值得关注的重要事件、经济数据或政策面变化'),
        ]

        def _bad(content):
            c = (content or '').strip()
            return (len(c) < 40) or ('不支持' in c) or ('无内容' in c) or ('请选择其他' in c)

        parts, ok = [], 0
        for title, fn, q in segments:
            try:
                r = fn(q) or {}
                content = r.get('content')
                if not r.get('error') and not _bad(content):
                    parts.append(f'━━━ {title} ━━━\n{content.strip()}')
                    ok += 1
            except Exception as se:
                print(f'[mx_weekend_outlook] {title} 失败: {se}')
        if not ok:
            _log_run(job, 'success', error='妙想无有效返回(可能限流/key)',
                     started_at=started, finished_at=datetime.now().isoformat())
            return
        body = f'🔮 周末妙想研判 — {datetime.now().strftime("%Y-%m-%d")}\n\n' + '\n\n'.join(parts)
        from notification_router import send
        send('report', '🔮 周末妙想研判', body)
        _log_run(job, 'success', error=f'segments={ok}/3',
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


def task_weekly_analysis():
    """🆕 周日持仓综合分析（真合并 weekly_portfolio_analysis + wf_weekly_portfolio_report，
    2026-06-12 整合:此前合并版丢了评级变化/浮盈明细/4象限体检,现补全后旧任务已删）"""
    job = 'weekly_analysis'
    started = datetime.now().isoformat()
    try:
        from portfolio_scheduler import portfolio_scheduler
        ok = portfolio_scheduler.run_once()

        lines = [f'━━━ 📊 本周持仓综合周报 — {datetime.now().strftime("%Y-%m-%d")} ━━━', '']

        # ─── 1. 持仓最新 AI 分析 ───
        from portfolio.portfolio_db import portfolio_db as pdb
        analysis = pdb.get_all_latest_analysis() or []
        sell_stocks, buy_stocks, hold_stocks = [], [], []
        for a in analysis:
            cost = float(a.get('cost_price', 0) or 0)
            price = float(a.get('current_price', 0) or 0)
            entry = {
                'code': a.get('code', ''), 'name': a.get('name', ''),
                'rating': a.get('rating', '持有'),
                'confidence': float(a.get('confidence', 0) or 0),
                'pnl': round((price - cost) / cost * 100, 1) if cost > 0 else 0,
                'cost': cost, 'price': price,
                'target': float(a.get('target_price', 0) or 0),
                'stop': float(a.get('stop_loss', 0) or 0),
                'stock_id': a.get('id', 0),
            }
            if entry['rating'] == '卖出':
                sell_stocks.append(entry)
            elif entry['rating'] in ('买入', '强烈买入'):
                buy_stocks.append(entry)
            else:
                hold_stocks.append(entry)

        # ─── 2. 评级变化追踪（近 8 天） ───
        lines.append('━━━ 📈 本周评级变化追踪 ━━━')
        changes_found = 0
        for s in analysis:
            try:
                for chg in pdb.get_rating_changes(s.get('id', 0), days=8):
                    t, old_r, new_r = chg
                    if changes_found < 15:
                        arrow = '⬆' if new_r in ('买入', '强烈买入') else '⬇'
                        lines.append(f"  {arrow} {s.get('code', '')} {s.get('name', '')}: {old_r} → {new_r}")
                        changes_found += 1
            except Exception:
                pass
        if changes_found == 0:
            lines.append('  （本周无评级变化）')
        lines.append('')

        # ─── 3. 建议减仓 Top5（信心+浮亏排序，带成本/现价/止损） ───
        lines.append('━━━ 🔴 建议减仓 Top5 ━━━')
        sell_stocks.sort(key=lambda x: (-x['confidence'], x['pnl'] if x['price'] > 0 else 999))
        sell_show = [s for s in sell_stocks
                     if s['confidence'] >= 8 or (s['price'] > 0 and s['pnl'] <= -5)] or sell_stocks[:5]
        for i, s in enumerate(sell_show[:5], 1):
            has_price = s['price'] > 0
            pnl_icon = '🔴' if (has_price and s['pnl'] < -10) else '🟡' if (has_price and s['pnl'] < -3) else '⚪'
            lines.append(f"  {i}. {pnl_icon} {s['code']} {s['name']}  信心{s['confidence']:.0f}  "
                         f"成本{s['cost']:.3f}→{s['price']:.3f}" if has_price else
                         f"  {i}. ⚪ {s['code']} {s['name']}  信心{s['confidence']:.0f}  （价格数据缺失）")
            if has_price:
                lines[-1] += f"  **{s['pnl']:+.1f}%**" + (f"  止损{s['stop']:.2f}" if s['stop'] else '')
        if not sell_show:
            lines.append('  （当前无卖出信号）')
        lines.append('')

        # ─── 4. 建议加仓 Top5 ───
        lines.append('━━━ 🟢 建议加仓 Top5 ━━━')
        buy_stocks.sort(key=lambda x: -x['confidence'])
        for i, s in enumerate(buy_stocks[:5], 1):
            price_display = f"{s['price']:.3f}" if s['price'] > 0 else '数据缺失'
            extra = (f" 目标{s['target']:.2f}" if s['target'] else '') + (f" 止损{s['stop']:.2f}" if s['stop'] else '')
            lines.append(f"  {i}. 🟢 {s['code']} {s['name']}  信心{s['confidence']:.0f}  现价{price_display} {extra}")
        if not buy_stocks:
            lines.append('  （当前无买入信号）')
        lines.append('')

        # ─── 5. 组合体检 X-Ray(规则引擎:集中度/质地/风险/结构) ───
        try:
            from portfolio_rules import run_check, format_text as _xray_fmt
            lines.append(_xray_fmt(run_check()))
            lines.append('')
        except Exception:
            pass

        # ─── 5a2. 组合绩效(TWR/XIRR/风险/归因)+ 基准对比 ───
        try:
            from portfolio.performance import summary as _perf_sum, format_text as _perf_fmt
            lines.append(_perf_fmt(_perf_sum()))
            from portfolio.benchmark import compare as _bench_cmp, format_text as _bench_fmt
            _bt = _bench_fmt(_bench_cmp("000300"))
            if _bt:
                lines.append(_bt)
            try:
                from analysis.monte_carlo import simulate as _mc, format_text as _mc_fmt
                _ms = _mc(horizon=60)
                if not _ms.get('error'):
                    lines.append(_mc_fmt(_ms))
            except Exception:
                pass
            lines.append('')
        except Exception:
            pass

        # ─── 5b. 已实现盈亏(本周 + 累计,来自真实成交记录) ───
        try:
            from portfolio.realized_pnl import backfill as _rp_backfill, summary as _rp_sum, format_text as _rp_fmt
            try:
                _rp_backfill()
            except Exception:
                pass
            # 交易行为诊断(影子账户)
            try:
                from analysis.shadow_account import run_diagnose as _sa_diag, format_text as _sa_fmt
                _sa = _sa_diag()
                if not _sa.get('error'):
                    lines.append(_sa_fmt(_sa))
                    lines.append('')
            except Exception:
                pass

            lines.append('━━━ 💰 已实现盈亏 ━━━')
            lines.append(_rp_fmt(_rp_sum(days=7), top_n=3))
            _all = _rp_sum()
            if _all.get('count'):
                lines.append(f"  (累计: {_all['total']:+,.0f}元 / {_all['count']}笔 / 胜率{_all['win_rate']}%)")
            lines.append('')
        except Exception:
            pass

        # ─── 6. 概览 + 周末新闻 ───
        lines.append('━━━ 📊 持仓概览 ━━━')
        lines.append(f"  共 {len(analysis)} 只持仓 | 🟢买入 {len(buy_stocks)} | "
                     f"✅持有 {len(hold_stocks)} | 🔴卖出 {len(sell_stocks)}")
        lines.append('')
        lines.append('━━━ 📰 周末/隔夜新闻影响 ━━━')
        try:
            news = datahub.market_news(15)
            for n in (news or [])[:8]:
                title = (n.get('title') or n.get('content', ''))[:60]
                t = n.get('time', '')[:16] if n.get('time') else ''
                lines.append(f"  [{t}] {title}")
            if not news:
                lines.append('  （暂无新闻数据）')
        except Exception:
            lines.append('  （新闻拉取暂不可用）')

        _push_archive('📊 本周持仓综合周报', '\n'.join(lines))
        _log_run(job, 'success' if ok else 'error',
                 error=f'buy={len(buy_stocks)} sell={len(sell_stocks)} changes={changes_found}'
                       + ('' if ok else ' (run_once False)'),
                 started_at=started, finished_at=datetime.now().isoformat())

    except Exception as e:
        _log_run(job, 'error', error=str(e), started_at=started,
                 finished_at=datetime.now().isoformat())


# 默认注册一组适合大多数用户的任务时间表
_REGISTERED = False


def _format_sector_rotation(pred: dict) -> str:
    """把智策引擎 final_predictions 收成一条简洁的题材轮动推送(看多空/轮动潜力/热度/关键机会)。

    pred 形状(sector_strategy_engine.run_comprehensive_analysis 产出):
      long_short: {bullish:[{sector,confidence}], bearish:[...]}
      rotation:   {potential:[{sector,advice}]}
      heat:       {hottest:[{sector,score}]}
      summary:    {key_opportunity: str}
    无有效内容返回空串(调用方据此不推送)。"""
    if not pred:
        return ''
    lines = []
    ls = pred.get('long_short') or {}
    bull = [it for it in (ls.get('bullish') or []) if (it.get('confidence') or 0) >= 7][:3]
    bear = [it for it in (ls.get('bearish') or []) if (it.get('confidence') or 0) >= 7][:3]
    if bull or bear:
        lines.append('━━ 多空研判 ━━')
        if bull:
            lines.append('🔴 看多: ' + '、'.join(f"{it.get('sector')}({it.get('confidence')}分)" for it in bull))
        if bear:
            lines.append('🟢 看空: ' + '、'.join(f"{it.get('sector')}({it.get('confidence')}分)" for it in bear))
    rot = (pred.get('rotation') or {}).get('potential') or []
    if rot:
        lines.append('\n━━ 轮动潜力 ━━')
        for it in rot[:3]:
            adv = (it.get('advice') or '').strip()
            lines.append(f"• {it.get('sector')}: {adv[:60]}" if adv else f"• {it.get('sector')}")
    hot = (pred.get('heat') or {}).get('hottest') or []
    if hot:
        lines.append('\n━━ 热度榜 ━━')
        lines.append('、'.join(f"{i}.{it.get('sector')}({it.get('score', 0)}分)"
                              for i, it in enumerate(hot[:3], 1)))
    opp = ((pred.get('summary') or {}).get('key_opportunity') or '').strip()
    if opp:
        lines.append('\n💡 ' + (opp[:160] + '…' if len(opp) > 160 else opp))
    return '\n'.join(lines).strip()


def task_sector_rotation():
    """📈 题材轮动雷达(盘后 17:30)—— 智策板块引擎(原 sector agents 子系统,自带 schedule 线程,
    现并入 jobs 统一节奏):盘后看多/看空板块 + 轮动潜力 + 热度榜 + 关键机会,一条推送看清今日资金
    在哪些题材间轮动、明日关注谁。开关 sector_rotation(默认开)。非交易日跳过(板块资金流需交易日)。"""
    job = 'sector_rotation'
    try:
        from automation_config import is_enabled
        if not is_enabled(job):
            _log_run(job, 'skipped', error='disabled', started_at=datetime.now().isoformat(),
                     finished_at=datetime.now().isoformat())
            return
    except Exception:
        pass
    if _skip_if_not_trading(job):
        return
    started = datetime.now().isoformat()
    try:
        from sector_strategy_data import SectorStrategyDataFetcher
        from sector_strategy_engine import SectorStrategyEngine
        data = SectorStrategyDataFetcher().get_all_sector_data()
        if not data.get('success'):
            # 外部板块数据暂不可用:平和收尾,不推吓人告警(非代码 bug)
            _log_run(job, 'success', error='板块数据暂不可用,跳过推送',
                     started_at=started, finished_at=datetime.now().isoformat())
            return
        result = SectorStrategyEngine().run_comprehensive_analysis(data)
        if not result.get('success'):
            _log_run(job, 'success', error='AI 分析未产出,跳过推送',
                     started_at=started, finished_at=datetime.now().isoformat())
            return
        body = _format_sector_rotation(result.get('final_predictions', {}))
        if body:
            try:
                from notification_router import send
                send('report', '📈 题材轮动雷达', body)
            except Exception as ne:
                print(f'[sector_rotation] 推送失败: {ne}')
        _log_run(job, 'success', error=(None if body else '无有效轮动结论'),
                 started_at=started, finished_at=datetime.now().isoformat())
    except Exception as e:
        _log_run(job, 'error', error=str(e),
                 started_at=started, finished_at=datetime.now().isoformat())


def register_default_jobs():
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True

    """注册整合后的任务时间表（2026-06-12 二次整合，CST 时区）

    时间表（CST，2026-06-25 大改:监控重构 + 盘后全挪 16:30 后）：
      08:55 fund_dca_reminder           — 定投提醒
      09:00 morning_strategy            — 📊 晨间市场报告(AI研判/新闻/数据快照,零逐只接口)
      09:05 fund_valuation_signal       — 估值信号
      09:45 unified_selection           — 整合选股(5策略+InStock13+多因子并池;尾接红蓝对抗+候选池)
      09:50 morning_portfolio           — ☀️ 早盘持仓分析 + 挑今日 top15 重点候选(存 focus_candidates)
      10:30 mx_selection_review         — 选股过妙想诊断(D:只在与综合选股分歧时推) + 急跌兜底
      11:20 noon_portfolio              — 🕦 午间盯盘(只看早盘候选) + 持仓急跌兜底
      12:00 noon_report                 — 📊 午盘简报(大盘)
      14:30 afternoon_portfolio         — 🧹 尾盘持仓总结(eod_review 三合一;尾接止盈阶梯减仓 + 急跌兜底)
      —— E:盘中急跌兜底覆盖 10:30/11:20/14:30 三点(_intraday_plunge_check,每股每日去重)——
      —— 盘后(全 16:30 后;F:读暖缓存任务显式等 kline_prefetch 焐完,不靠时钟间隔)——
      16:30 kline_prefetch              — 📥 K线+因子缓存预热(链头)
      16:40 factor_collection           — 🧬 因子采集(读暖缓存,F:等 prefetch)
      16:45 portfolio_indicator_snapshot— 📸 持仓指标快照(MyTT/缠论/VaR;F:等 prefetch;次日早盘读它)
      16:48 daily_market_snapshot       — 📷 大盘快照
      16:55 eod_outcomes                — 🎯 盘后后验合并(A:推荐池回填 + 决策信号后验,F:等 prefetch)
      18:30 dragon_tiger_archive        — 🐉 龙虎榜归档(晚间出全量)
      18:35 announcement_scan           — 📢 公告/研报/解禁三合一预警
      19:00 daily_backtest              — 📐 回测+基因组进化(尾接策略扫描→推荐池)
      17:00 mx_daily_analysis           — 🌙 妙想收盘复盘
      17:30 sector_rotation             — 📈 题材轮动雷达(智策板块引擎进每日节奏)
      22:00 fund_evening                — 🏦 基金晚间合并(B:净值入库→定投止盈检查) · 22:30 daily_pnl_snapshot
      02:00/02:30 pg_backup / rag_ingest
      周日 10:00/15:00/16:00/20:00 mx_weekend_outlook / weekly_analysis / portfolio_stress_ai / wf_weekly_backtest
      周一 03:00/09:30 weekly_db_cleanup / ai_eval_weekly
      ⚠️ 退役(不再注册):stock_monitor_check(进场区间盯盘,价值低→急跌并入 noon_portfolio);
         selection_debate/lockup_radar/research_digest(已并入 unified_selection / announcement_scan)。

    2026-06-12 整合说明:
      已删除(被覆盖): morning_briefing_push(并入 morning_strategy)、morning_warmup(并入快照)、
        northbound_flow_refresh(读时自刷新)、strategy_screening/morning_picks(unified_selection)、
        dragon_tiger_report(morning_strategy 模块C)、afternoon_picks(afternoon_portfolio)、
        chan_scan(快照+早盘)、portfolio_risk/daily_pattern_alert(快照尾部)、
        multi_factor_screen(unified_selection 同缓存)、weekly_portfolio_report/
        weekly_portfolio_analysis(weekly_analysis)。
      改为子流程(开关仍有效): _daily_strategy_scan、_daily_candidate_pool、
        _position_profit_check、_position_guard_check。
      2026-06-12 二轮: morning_pnl(08:50昨日收益)并入 morning_strategy 模块D;
        持仓扫描改读盘后快照(不再逐只拉K线);AI 加 lazy_summary 口语化一句话。
    """
    # ---- 🟢 盘前 ----
    hub.register('morning_strategy',            '09:00', task_morning_strategy)
    hub.register('fund_dca_reminder',           '08:55', task_fund_dca_reminder)
    hub.register('fund_valuation_signal',       '09:05', task_fund_valuation_signal)

    # ---- 09:45 整合选股 ----
    hub.register('main_force_prefetch',         '09:15', task_main_force_prefetch)  # 盘前预取主力选股问财结果入缓存
    hub.register('unified_selection',           '09:45', task_unified_selection)
    # ---- 持仓分析三点(2026-06-25):早盘挑候选 → 午间只看候选 → 尾盘全局总结。持仓多(80只)
    #      不再全程逐只盯,聚焦早盘挑的 top15。红蓝对抗已并入 unified_selection(原 selection_debate@10:00 删)。
    hub.register('morning_portfolio',           '09:50', task_morning_portfolio)   # 早盘:全持仓 + 挑 top15 候选
    hub.register('mx_selection_review',         '10:30', task_mx_selection_review)
    hub.register('noon_portfolio',              '11:20', task_noon_portfolio)      # 午间:只盯早盘候选 + 急跌兜底

    # ---- 🟡 盘中 ----
    hub.register('noon_report',                 '12:00', task_noon_report)
    # ⚠️ 2026-06-25 监控重构:stock_monitor_check(进场区间盯盘,价值低)已退役、不再注册;
    #    其急跌兜底并入 11:20 noon_portfolio;ai_rec_check(推荐池胜率回填,非盯盘)由 every:30 → 盘后 16:35 收盘后验。
    # ---- 14:30 尾盘持仓总结(eod_review 三合一,从 14:40 提前) ----
    hub.register('afternoon_portfolio',          '14:30', task_afternoon_portfolio)

    # ---- 🔴 盘后(2026-06-25 全部挪到 16:30 之后:错峰 + 收盘数据已稳,降东财峰值并发)----
    # ⚠️ 硬依赖顺序(勿随手按字母/时间调序):kline_prefetch 必须**最先**焐 K线+因子缓存,
    #    factor_collection / portfolio_indicator_snapshot / daily_backtest 都读这份暖缓存(顺序颠倒→冷拉雪崩);
    #    decision_signal_outcomes 需当日收盘 K线已入缓存;龙虎榜傍晚才出全量,故 dragon_tiger 押后到 18:30。
    hub.register('kline_prefetch',              '16:30', task_kline_prefetch)            # 链头:焐缓存(最慢,留足窗)
    hub.register('factor_collection',           '16:40', task_factor_collection)        # 读暖缓存(F:等 prefetch 焐完)
    hub.register('portfolio_indicator_snapshot','16:45', task_portfolio_indicator_snapshot)
    hub.register('daily_market_snapshot',       '16:48', task_daily_market_snapshot)
    # A 合并(2026-06-26):eod_outcomes = 原 ai_rec_check(16:35 推荐池回填)+ decision_signal_outcomes(信号后验)
    hub.register('eod_outcomes',                '16:55', task_eod_outcomes)             # 盘后后验合并(读暖缓存)
    hub.register('dragon_tiger_archive',        '18:30', task_dragon_tiger_archive)     # 龙虎榜晚间才出全量
    hub.register('announcement_scan',           '18:35', task_announcement_scan)        # 公告/研报/解禁三合一

    # ---- 📐 盘后回测(放最后:纯 CPU 重活,读全量暖缓存,不与焐缓存抢)----
    hub.register('daily_backtest',             '19:00', task_daily_backtest)

    # ---- 🌙 夜间 ----
    hub.register('mx_daily_analysis',           '17:00', task_mx_daily_analysis)
    hub.register('sector_rotation',             '17:30', task_sector_rotation)        # 📈 题材轮动雷达(盘后,智策引擎)
    hub.register('daily_pnl_snapshot',          '22:30', task_daily_pnl_snapshot)
    # B 合并(2026-06-26):fund_evening = 原 fund_nav_refresh(22:00 净值入库)→ fund_target_check(止盈检查)
    hub.register('fund_evening',                '22:00', task_fund_evening)             # 基金晚间合并(先刷净值再查止盈)
    hub.register('pg_backup',                   '02:00', task_pg_backup)
    hub.register('rag_ingest',                  '02:30', task_rag_ingest)

    # ---- 📅 周日 ----
    try:
        wrapped = hub._wrap('mx_weekend_outlook', task_mx_weekend_outlook)
        job = schedule.every().sunday.at('10:00').do(wrapped)
        hub._registered.append({
            'name': 'mx_weekend_outlook', 'when': 'sun 10:00 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] mx_weekend_outlook 注册失败: {e}')

    try:
        wrapped = hub._wrap('weekly_analysis', task_weekly_analysis)
        job = schedule.every().sunday.at('15:00').do(wrapped)
        hub._registered.append({
            'name': 'weekly_analysis', 'when': 'sun 15:00 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] weekly_analysis 注册失败: {e}')

    try:
        wrapped = hub._wrap('portfolio_stress_ai', task_portfolio_stress_ai)
        job = schedule.every().sunday.at('16:00').do(wrapped)
        hub._registered.append({
            'name': 'portfolio_stress_ai', 'when': 'sun 16:00 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] portfolio_stress_ai 注册失败: {e}')

    try:
        wrapped = hub._wrap('weekly_db_cleanup', task_weekly_db_cleanup)
        job = schedule.every().monday.at('03:00').do(wrapped)
        hub._registered.append({
            'name': 'weekly_db_cleanup', 'when': 'mon 03:00 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] weekly_db_cleanup 注册失败: {e}')

    try:
        wrapped = hub._wrap('wf_weekly_backtest', task_weekly_backtest)
        job = schedule.every().sunday.at('20:00').do(wrapped)
        hub._registered.append({
            'name': 'wf_weekly_backtest', 'when': 'sun 20:00 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] wf_weekly_backtest 注册失败: {e}')

    # ---- 📅 周一 ----
    try:
        wrapped = hub._wrap('ai_eval_weekly', task_ai_eval_weekly)
        job = schedule.every().monday.at('09:30').do(wrapped)
        hub._registered.append({
            'name': 'ai_eval_weekly', 'when': 'mon 09:30 CST', 'job': job,
        })
    except Exception as e:
        print(f'[jobs_hub] ai_eval_weekly 注册失败: {e}')


def serve_forever():
    """独立运行模式：注册任务并保持主线程运行
    供 watchdog 或 entrypoint 直接启动，不依赖 Streamlit daemon thread
    """
    import time
    register_default_jobs()
    print(f'[jobs_hub] 🚀 独立模式启动, {len(hub.list_jobs())} jobs 已注册', flush=True)
    hub.start()
    # 主线程保持存活
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print('[jobs_hub] 收到退出信号', flush=True)
        hub.stop()
    except BaseException as e:
        # SystemExit / C 扩展异常等 BaseException 子类: 不静默吞, 打 traceback 后
        # 抛出去, 让 supervisor 看到非 0 退出码并 autorestart。
        import traceback
        print(f'[jobs_hub] 主线程异常退出: {type(e).__name__}: {e}', flush=True)
        traceback.print_exc()
        raise


if __name__ == '__main__':
    import sys
    if '--serve' in sys.argv:
        serve_forever()
    else:
        print('=== Jobs Hub 自检 ===')
        print(f'snapshot db: {_SNAPSHOT_DB_PATH}')
        register_default_jobs()
        print('registered:', hub.list_jobs())
        print('recent runs:', hub.list_recent_runs(5))
        # 立即跑一次大盘快照
        print('\n触发 daily_market_snapshot...')
        task_daily_market_snapshot()
        snap = get_market_snapshot()
        if snap:
            print('  north_flow rows:', len(snap.get('north_flow', [])))
            print('  dragon_tiger rows:', len(snap.get('dragon_tiger', [])))
        print('OK')
