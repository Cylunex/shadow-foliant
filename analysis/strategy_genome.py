"""
策略基因组引擎 — 策略参数化 + 进化 + 全局评分

三张 PG 表（自建）：
  strategy_variants  — 策略变体（base + params + generation + fitness）
  strategy_scores    — 每日横截面评分（跨股池聚合）
  stock_strategy_affinity — 个股策略适配度

进化流程（16:30 执行）：
  1. 全股池回测（持仓 + 候选池 TOP30）
  2. 横截面聚合 → strategy_scores
  3. 参数变异 → 新变体 → 回测 → 优存劣汰
  4. 个股适配度更新 → stock_strategy_affinity
"""

import json
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

import psycopg2
import psycopg2.extras

from core.database_pg import get_conn

# ══════════════════════════════════════════════════════════
#  策略参数空间 — (min, max, default_precision, step, description)
#  default 是原 InStock 硬编码值
# ══════════════════════════════════════════════════════════

STRATEGY_PARAM_SPACE = {
    'enter': {
        'threshold':       (30,   120,  5,    '回溯天数'),
        'pct_change_min':  (1.0,  6.0,  0.5,  '最小涨幅%'),
        'amount_min_yi':   (1.0,  8.0,  0.5,  '最低成交额(亿)'),
        'vol_ratio_min':   (1.3,  6.0,  0.3,  '最低量比'),
    },
    'keep_increasing': {
        'threshold':       (15,   90,   5,    '回溯天数'),
        'ma_period':       (10,   90,   5,    'MA周期'),
        'ratio_min':       (1.02, 1.8,  0.02, 'MA涨幅最低倍'),
    },
    'turtle_trade': {
        'threshold':       (15,   180,  5,    '新高回调天数'),
    },
    'parking_apron': {
        'threshold':       (10,   30,   1,    '回溯天数'),
        'surge_pct_min':   (6.0,  9.8,  0.5,  '放量涨幅最低%'),
        'gap_tol_pct':     (1.0,  8.0,  0.5,  '整理振幅容差%'),
        'consol_pct_range':(2.5, 8.0,  0.5,  '整理日波动范围%'),
    },
    'low_atr': {
        'ma_long':         (120,  365,  10,   '上市天数'),
        'threshold':       (5,    30,   1,    '回溯天数'),
        'atr_max_pct':     (3.0,  15.0, 0.5,  '最大ATR%'),
        'ratio_min':       (1.02, 1.3,  0.01, '高低比最低'),
    },
    'high_tight_flag': {
        'threshold':       (30,   120,  10,   '回溯天数'),
        'ratio_min':       (1.3,  3.0,  0.1,  '旗形比率最低'),
        'surge_pct_min':   (6.0,  9.8,  0.5,  '连续涨停最低%'),
    },
    'breakthrough_platform': {
        'threshold':       (30,   120,  10,   '回溯天数'),
        'ma_period':       (20,   120,  10,   'MA周期'),
        'deviation_low':   (-8.0, -1.0, 0.5,  '偏离下限%'),
        'deviation_high':  (10.0, 35.0, 1.0,  '偏离上限%'),
    },
    'backtrace_ma250': {
        'threshold':       (40,   120,  10,   '回溯天数'),
        'ma_period':       (120,  365,  10,   'MA周期'),
        'vol_ratio_min':   (1.3,  5.0,  0.3,  '缩量比最低'),
        'back_ratio_max':  (0.6,  0.92, 0.02, '回踩比最高'),
        'date_diff_low':   (5,    20,   2,    '时间间隔下限'),
        'date_diff_high':  (30,   80,   5,    '时间间隔上限'),
    },
    'climax_limitdown': {
        'threshold':       (30,   120,  5,    '回溯天数'),
        'drop_pct_min':    (-9.8, -5.0, 0.5,  '最小跌幅%'),
        'amount_min_yi':   (0.5,  5.0,  0.5,  '最低成交额(亿)'),
        'vol_ratio_min':   (2.0,  8.0,  0.5,  '最低量比'),
    },
    'low_backtrace_increase': {
        'threshold':       (30,   120,  10,   '回溯天数'),
        'ratio_min':       (0.3,  1.2,  0.05, '涨幅比最低'),
        'max_single_drop': (-12.0, -3.0, 0.5, '单日最大跌幅%'),
        'max_two_day_drop':(-18.0, -5.0, 1.0, '两日最大跌幅%'),
    },
    'rsi_oversold_bounce': {
        'threshold':          (30,   120,  10,   '回溯天数'),
        'oversold_threshold': (20,   40,   2,    'RSI超卖阈值'),
        'min_days':           (1,    5,    1,    '超卖持续天数'),
        'vol_ratio_min':      (1.0,  3.0,  0.2,  '最低量比'),
        'max_days':           (3,    15,   1,    '新低回溯天数'),
    },
    'bollinger_squeeze_breakout': {
        'threshold':       (60,   200,  10,   '回溯天数'),
        'bb_period':       (10,   30,   2,    '布林带周期'),
        'bb_std':          (1.5,  3.0,  0.1,  '标准差倍数'),
        'sqz_percentile':  (10,   40,   5,    '压缩百分位%'),
        'min_sqz_days':    (2,    8,    1,    '最小压缩天数'),
        'vol_ratio_min':   (1.2,  3.0,  0.2,  '最低量比'),
        'break_ret_min':   (0.5,  3.0,  0.3,  '最小突破涨幅%'),
    },
    'weekly_trend_daily_signal': {
        'threshold':        (60,   250,  10,   '回溯天数'),
        'weekly_ma_period': (5,    20,   1,    '周线MA周期'),
        'daily_ma_period':  (5,    30,   2,    '日线均量周期'),
        'vol_ratio_min':    (1.2,  3.0,  0.2,  '最低量比'),
        'daily_ret_min':    (1.0,  4.0,  0.3,  '最小日涨幅%'),
        'breakout_days':    (3,    15,   1,    '突破天数'),
    },
}

# 持有期参与进化(2026-06-12):所有策略的参数空间统一追加 hold_days,
# 回测按变体自己的持有期评估——短线突破和趋势策略的最优持有期天差地别,不该写死10天。
# (不是策略函数参数,由 daily_backtest 取出传给 backtest_one;签名过滤会自动忽略它)
for _sid in STRATEGY_PARAM_SPACE:
    STRATEGY_PARAM_SPACE[_sid].setdefault('hold_days', (3, 30, 1, '持有天数'))

# 老变体没有的新空间参数,变异时用这里的默认值补齐后参与扰动(逐代进入基因池)
_MISSING_PARAM_DEFAULTS = {'hold_days': 10}


def default_params(strategy_id: str) -> Dict[str, Any]:
    """返回策略的默认参数（原 InStock 硬编码值）"""
    defaults = {
        'enter':                  {'threshold': 60, 'pct_change_min': 2.0, 'amount_min_yi': 2.0, 'vol_ratio_min': 2.0},
        'keep_increasing':        {'threshold': 30, 'ma_period': 30, 'ratio_min': 1.2},
        'turtle_trade':           {'threshold': 60},
        'parking_apron':          {'threshold': 15, 'surge_pct_min': 9.5, 'gap_tol_pct': 3.0, 'consol_pct_range': 5.0},
        'low_atr':                {'ma_long': 250, 'threshold': 10, 'atr_max_pct': 10.0, 'ratio_min': 1.1},
        'high_tight_flag':        {'threshold': 60, 'ratio_min': 1.9, 'surge_pct_min': 9.5},
        'breakthrough_platform':  {'threshold': 60, 'ma_period': 60, 'deviation_low': -5.0, 'deviation_high': 20.0},
        'backtrace_ma250':        {'threshold': 60, 'ma_period': 250, 'vol_ratio_min': 2.0, 'back_ratio_max': 0.8, 'date_diff_low': 10, 'date_diff_high': 50},
        'climax_limitdown':       {'threshold': 60, 'drop_pct_min': -9.5, 'amount_min_yi': 2.0, 'vol_ratio_min': 4.0},
        'low_backtrace_increase': {'threshold': 60, 'ratio_min': 0.6, 'max_single_drop': -7.0, 'max_two_day_drop': -10.0},
        'rsi_oversold_bounce':   {'threshold': 60, 'oversold_threshold': 30, 'min_days': 2, 'vol_ratio_min': 1.2, 'max_days': 5},
        'bollinger_squeeze_breakout': {'threshold': 100, 'bb_period': 20, 'bb_std': 2.0, 'sqz_percentile': 20, 'min_sqz_days': 3, 'vol_ratio_min': 1.5, 'break_ret_min': 1.0},
        'weekly_trend_daily_signal': {'threshold': 120, 'weekly_ma_period': 10, 'daily_ma_period': 10, 'vol_ratio_min': 1.5, 'daily_ret_min': 1.5, 'breakout_days': 5},
    }
    return defaults.get(strategy_id, {})


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _quantize(val, lo, hi, step):
    """按参数空间步长量化:对齐到 lo + n*step,并保持整型参数为 int
    (修复:原变异产出连续浮点,天数/周期类参数变成 57.3 之类,策略内切片报错或被截断)"""
    if step:
        val = round((val - lo) / step) * step + lo
    val = _clamp(val, lo, hi)
    if float(step).is_integer() and float(lo).is_integer() and float(hi).is_integer():
        return int(round(val))
    return round(val, 6)


def coerce_params(strategy_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """把(可能来自旧库的连续浮点)参数对齐到参数空间网格,幂等"""
    space = STRATEGY_PARAM_SPACE.get(strategy_id, {})
    out = {}
    for key, val in params.items():
        spec = space.get(key)
        if spec is None or not isinstance(val, (int, float)):
            out[key] = val
            continue
        out[key] = _quantize(float(val), spec[0], spec[1], spec[2])
    return out


def mutate_params(strategy_id: str, base_params: Dict[str, Any],
                  mutation_strength: float = 0.25) -> Dict[str, Any]:
    """参数变异 — 随机扰动 ±mutation_strength 范围内,按步长量化。
    参数空间里有而父代缺的键(如后加的 hold_days)用默认值补齐再变异,新参数逐代进入基因池。"""
    space = STRATEGY_PARAM_SPACE.get(strategy_id, {})
    merged = dict(base_params)
    _defaults = default_params(strategy_id)
    for key in space:
        if key not in merged:
            spec = space[key]
            merged[key] = _defaults.get(key, _MISSING_PARAM_DEFAULTS.get(
                key, (spec[0] + spec[1]) / 2))
    new = {}
    for key, val in merged.items():
        spec = space.get(key)
        if spec is None:
            new[key] = val
            continue
        lo, hi, step = spec[0], spec[1], spec[2]
        delta = (hi - lo) * mutation_strength * (random.random() * 2 - 1)
        new[key] = _quantize(float(val) + delta, lo, hi, step)
    return new


def crossover_params(p1: Dict[str, Any], p2: Dict[str, Any]) -> Dict[str, Any]:
    """参数交叉 — 每个 key 随机选父代"""
    child = {}
    for key in p1:
        child[key] = p1[key] if random.random() < 0.5 else p2.get(key, p1[key])
    return child


# ══════════════════════════════════════════════════════════
#  评分函数
# ══════════════════════════════════════════════════════════

# 风险项阈值:平均最大回撤(绝对值%)达到该值则风险得分归零。短持有期(10~30 日)的
# avg_max_dd 多落在 5~20%,取 20% 作满罚阈值,使变体间回撤差异有足够区分度。可调。
_RISK_DD_FULL_PENALTY = 20.0

# 小样本置信阈值:触发数 ≥ 该值 = 满置信(不缩分);低于则按 trigger/该值 比例把整分收缩向 0。
# 根治"2 笔交易 100% 胜率 → 评分 85 霸榜情报排行 → 0.85 实盘选股权重"的小样本幻觉:
# holdout 部署门(holdout_trigger≥3)只拦"是否部署",拦不住情报排行 / strategy_weights / AI 提示
# 用的 score 本身,故必须在 score 上做置信收缩。对触发充足的策略零影响,只压低小样本侥幸值。可调。
_MIN_TRIGGERS_FULL_CONF = 8


def compute_strategy_score(win_rate: float, avg_ret: float,
                           trigger_count: int, max_trigger: int = 1,
                           sample_stocks: int = 1, max_dd: float = 0.0) -> float:
    """综合评分 0~100(×小样本置信收缩)

    权重(2026-06-28 加入风险项):
      - 胜率 35%（核心）
      - 均收益 25%（涨得多少）
      - 风险 20%（平均最大回撤越深得分越低；max_dd 此前算了存了却从不进评分→进化系统性
        偏好"高胜率高收益但深回撤"的过拟合变体,这是最伤的缺口）
      - 触发率 10%（信号多不多，避免"胜率高但一年就一次"的过拟合）
      - 样本量 10%（覆盖的股票数 / 30，避免小样本幻觉）
    最后乘**置信收缩** min(1, trigger/_MIN_TRIGGERS_FULL_CONF):触发太少则整分缩向 0,
    防小样本侥幸值(如 2 笔 100% 胜率)霸榜情报/选股权重(触发充足者不受影响)。

    max_dd: 平均最大回撤百分比(负值,如 -12.5;传 0 = 无回撤数据 → 风险项满分,向后兼容)。
    """
    if max_trigger <= 0:
        max_trigger = 1
    wr = min(1.0, max(0.0, win_rate / 100.0))
    ar_norm = min(1.0, max(0.0, (avg_ret + 5) / 15))  # -5%→0, 10%→1
    trig_norm = min(1.0, trigger_count / max_trigger) if max_trigger > 0 else 0
    sample_norm = min(1.0, sample_stocks / 30)
    # 回撤越深风险分越低:0 回撤→1.0,回撤≥阈值→0(线性)
    dd = abs(max_dd or 0.0)
    risk_norm = min(1.0, max(0.0, 1.0 - dd / _RISK_DD_FULL_PENALTY))

    score = (wr * 35 + ar_norm * 25 + risk_norm * 20 + trig_norm * 10 + sample_norm * 10)
    # 小样本置信收缩(乘性):触发数充足→×1 不变;过少→按比例缩向 0
    confidence = min(1.0, max(0, trigger_count) / _MIN_TRIGGERS_FULL_CONF)
    return round(score * confidence, 1)


# ══════════════════════════════════════════════════════════
#  数据库表管理
# ══════════════════════════════════════════════════════════

def init_genome_tables():
    """建表（幂等）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_variants (
            id              BIGSERIAL PRIMARY KEY,
            base_strategy   VARCHAR(64) NOT NULL,
            strategy_cn     VARCHAR(32),
            generation      INTEGER NOT NULL DEFAULT 0,
            params          JSONB NOT NULL DEFAULT '{}',
            win_rate_pct    DOUBLE PRECISION,
            avg_ret_pct     DOUBLE PRECISION,
            max_dd_pct      DOUBLE PRECISION,
            trigger_count   INTEGER,
            sample_stocks   INTEGER DEFAULT 0,
            score           DOUBLE PRECISION,
            status          VARCHAR(16) DEFAULT 'active',  -- active / promoted / retired
            parent_id       BIGINT,
            created_at      TIMESTAMP DEFAULT NOW(),
            evaluated_at    TIMESTAMP,
            UNIQUE(base_strategy, params)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS strategy_scores (
            id              BIGSERIAL PRIMARY KEY,
            strategy_id     VARCHAR(64) NOT NULL,
            variant_id      BIGINT REFERENCES strategy_variants(id),
            eval_date       DATE NOT NULL DEFAULT CURRENT_DATE,
            stock_pool_n    INTEGER DEFAULT 0,
            triggered_n     INTEGER DEFAULT 0,
            win_rate_pct    DOUBLE PRECISION,
            avg_ret_pct     DOUBLE PRECISION,
            max_dd_pct      DOUBLE PRECISION,
            best_ret_pct    DOUBLE PRECISION,
            worst_ret_pct   DOUBLE PRECISION,
            score           DOUBLE PRECISION,
            market_regime   VARCHAR(32),   -- bull / bear / range
            created_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(strategy_id, eval_date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_strategy_affinity (
            id              BIGSERIAL PRIMARY KEY,
            stock_code      VARCHAR(16) NOT NULL,
            strategy_id     VARCHAR(64) NOT NULL,
            win_rate_pct    DOUBLE PRECISION,
            avg_ret_pct     DOUBLE PRECISION,
            trigger_count   INTEGER DEFAULT 0,
            score           DOUBLE PRECISION,
            last_eval_date  DATE NOT NULL DEFAULT CURRENT_DATE,
            updated_at      TIMESTAMP DEFAULT NOW(),
            UNIQUE(stock_code, strategy_id)
        )
    """)
    # 进化效果闭环:每次组合级 walk-forward A/B(进化集 vs 全默认集,同池同期)的结果。
    # excess_*>0 = 进化集更优。get_live_strategy_set 据近 N 条 excess_return 均值决定是否自动回退默认。
    cur.execute("""
        CREATE TABLE IF NOT EXISTS evolution_ab (
            id                 BIGSERIAL PRIMARY KEY,
            eval_date          DATE NOT NULL DEFAULT CURRENT_DATE,
            period_start       DATE,
            period_end         DATE,
            pool_n             INTEGER DEFAULT 0,
            evolved_n_strat    INTEGER DEFAULT 0,
            evolved_return_pct DOUBLE PRECISION,
            default_return_pct DOUBLE PRECISION,
            excess_return_pct  DOUBLE PRECISION,
            evolved_cagr_pct   DOUBLE PRECISION,
            default_cagr_pct   DOUBLE PRECISION,
            evolved_sharpe     DOUBLE PRECISION,
            default_sharpe     DOUBLE PRECISION,
            evolved_max_dd_pct DOUBLE PRECISION,
            default_max_dd_pct DOUBLE PRECISION,
            created_at         TIMESTAMP DEFAULT NOW(),
            UNIQUE(eval_date)
        )
    """)

    # 索引
    for idx in [
        "CREATE INDEX IF NOT EXISTS idx_sv_score ON strategy_variants(score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sv_status ON strategy_variants(base_strategy, status)",
        "CREATE INDEX IF NOT EXISTS idx_ss_date ON strategy_scores(eval_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ss_sid ON strategy_scores(strategy_id, eval_date DESC)",
        "CREATE INDEX IF NOT EXISTS idx_ssa_stock ON stock_strategy_affinity(stock_code, score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_evab_date ON evolution_ab(eval_date DESC)",
    ]:
        cur.execute(idx)

    # 样本外(walk-forward)列:holdout=近 N 日触发的真实表现,作 get_live_strategy_set 的上线门
    # (进化在 train 上广搜,只有 holdout 也成立的变体才部署,杜绝"全样本内 train=val=select"过拟合)
    for col in (
        "ALTER TABLE strategy_variants ADD COLUMN IF NOT EXISTS holdout_win_rate_pct DOUBLE PRECISION",
        "ALTER TABLE strategy_variants ADD COLUMN IF NOT EXISTS holdout_avg_ret_pct DOUBLE PRECISION",
        "ALTER TABLE strategy_variants ADD COLUMN IF NOT EXISTS holdout_trigger INTEGER DEFAULT 0",
        "ALTER TABLE strategy_variants ADD COLUMN IF NOT EXISTS holdout_eval_at TIMESTAMP",
    ):
        try:
            cur.execute(col)
        except Exception:
            pass

    conn.commit()
    cur.close()
    conn.close()


# 策略英文 id → 中文名(2026-07-02 提到模块级:进化日报等推送要用人话,别让用户读 'enter'/'low_atr')
STRATEGY_CN = {
    'enter': '放量上涨', 'keep_increasing': '均线多头', 'turtle_trade': '海龟交易',
    'parking_apron': '停机坪', 'low_atr': '低ATR成长', 'high_tight_flag': '高而窄旗形',
    'breakthrough_platform': '突破平台', 'backtrace_ma250': '回踩年线',
    'climax_limitdown': '放量跌停', 'low_backtrace_increase': '无大幅回撤',
    'rsi_oversold_bounce': 'RSI超卖反弹', 'bollinger_squeeze_breakout': '布林收窄突破',
    'weekly_trend_daily_signal': '周线趋势+日线',
}


def strategy_cn(sid: str) -> str:
    """策略 id 转中文展示名:基础策略查表;组合策略给统一中文前缀;其余保底原 id。"""
    s = str(sid or '')
    if s in STRATEGY_CN:
        return STRATEGY_CN[s]
    if s.startswith('composed'):
        return '组合策略'
    return s


def seed_default_variants():
    """首次运行时，把 10 套默认策略作为 generation=0 种子入库"""
    conn = get_conn()
    cur = conn.cursor()

    for sid in STRATEGY_PARAM_SPACE:
        params = default_params(sid)
        cur.execute("""
            INSERT INTO strategy_variants (base_strategy, strategy_cn, generation, params, status)
            VALUES (%s, %s, 0, %s, 'active')
            ON CONFLICT (base_strategy, params) DO NOTHING
        """, (sid, STRATEGY_CN.get(sid, sid), json.dumps(params)))

    conn.commit()
    cur.close()
    conn.close()
    return len(STRATEGY_PARAM_SPACE)


# ══════════════════════════════════════════════════════════
#  横截面评分（每次跑完回测后调用）
# ══════════════════════════════════════════════════════════

def save_strategy_score(strategy_id: str, variant_id: int,
                        stock_pool_n: int, triggered_n: int,
                        win_rate: float, avg_ret: float, max_dd: float,
                        best_ret: float, worst_ret: float,
                        sample_stocks: int = 1,
                        market_regime: str = None) -> int:
    """保存某策略在某日的横截面评分"""
    score = compute_strategy_score(win_rate, avg_ret, triggered_n,
                                   max_trigger=stock_pool_n,
                                   sample_stocks=sample_stocks, max_dd=max_dd)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategy_scores (strategy_id, variant_id, eval_date, stock_pool_n, triggered_n,
                                     win_rate_pct, avg_ret_pct, max_dd_pct, best_ret_pct, worst_ret_pct,
                                     score, market_regime)
        VALUES (%s, %s, CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (strategy_id, eval_date) DO UPDATE SET
            variant_id = EXCLUDED.variant_id,
            stock_pool_n = EXCLUDED.stock_pool_n,
            triggered_n = EXCLUDED.triggered_n,
            win_rate_pct = EXCLUDED.win_rate_pct,
            avg_ret_pct = EXCLUDED.avg_ret_pct,
            max_dd_pct = EXCLUDED.max_dd_pct,
            best_ret_pct = EXCLUDED.best_ret_pct,
            worst_ret_pct = EXCLUDED.worst_ret_pct,
            score = EXCLUDED.score,
            market_regime = EXCLUDED.market_regime
        RETURNING id
    """, (strategy_id, variant_id, stock_pool_n, triggered_n,
          win_rate, avg_ret, max_dd, best_ret, worst_ret, score, market_regime))
    sid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return sid


def update_variant_fitness(variant_id: int, win_rate: float, avg_ret: float,
                           max_dd: float, trigger_count: int,
                           sample_stocks: int = 1,
                           holdout_win_rate: float = None, holdout_avg_ret: float = None,
                           holdout_trigger: int = None):
    """更新变体适应度。win_rate/avg_ret = 训练集(driving 进化);holdout_* = 样本外(部署门用)。
    holdout 传 None 时不覆盖既有值(COALESCE)。"""
    score = compute_strategy_score(win_rate, avg_ret, trigger_count,
                                   max_trigger=sample_stocks,
                                   sample_stocks=sample_stocks, max_dd=max_dd)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE strategy_variants SET
            win_rate_pct = %s, avg_ret_pct = %s, max_dd_pct = %s,
            trigger_count = %s, sample_stocks = %s, score = %s,
            holdout_win_rate_pct = COALESCE(%s, holdout_win_rate_pct),
            holdout_avg_ret_pct  = COALESCE(%s, holdout_avg_ret_pct),
            holdout_trigger      = COALESCE(%s, holdout_trigger),
            holdout_eval_at = CASE WHEN %s IS NOT NULL THEN NOW() ELSE holdout_eval_at END,
            evaluated_at = NOW()
        WHERE id = %s
    """, (win_rate, avg_ret, max_dd, trigger_count, sample_stocks, score,
          holdout_win_rate, holdout_avg_ret, holdout_trigger, holdout_trigger, variant_id))
    conn.commit()
    cur.close()
    conn.close()


def update_stock_affinity(stock_code: str, strategy_id: str,
                          win_rate: float, avg_ret: float,
                          trigger_count: int, score: float):
    """更新个股策略适配度"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO stock_strategy_affinity (stock_code, strategy_id, win_rate_pct, avg_ret_pct,
                                             trigger_count, score, last_eval_date)
        VALUES (%s, %s, %s, %s, %s, %s, CURRENT_DATE)
        ON CONFLICT (stock_code, strategy_id) DO UPDATE SET
            win_rate_pct = EXCLUDED.win_rate_pct,
            avg_ret_pct = EXCLUDED.avg_ret_pct,
            trigger_count = EXCLUDED.trigger_count,
            score = EXCLUDED.score,
            last_eval_date = CURRENT_DATE,
            updated_at = NOW()
    """, (stock_code, strategy_id, win_rate, avg_ret, trigger_count, score))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  进化引擎
# ══════════════════════════════════════════════════════════

def get_active_variants(strategy_id: str = None, min_score: float = 0,
                        limit: int = 50) -> List[Dict]:
    """获取活跃变体"""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = "WHERE status = 'active'"
    params_vals = []
    if strategy_id:
        where += " AND base_strategy = %s"
        params_vals.append(strategy_id)

    cur.execute(f"""
        SELECT * FROM strategy_variants {where}
        ORDER BY score DESC NULLS LAST
        LIMIT %s
    """, (*params_vals, limit))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def get_variants_for_eval(strategy_id: str, top_n: int = 5, fresh_n: int = 5) -> List[Dict]:
    """取某策略本轮要回测的变体:已评估的高分 top_n + 未评估的新生 fresh_n。
    (修复:原 daily_backtest 全局 LIMIT 100 按分数排,未评估变体 NULLS LAST 永远轮不到 → 新变体从不被评估)"""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        (SELECT * FROM strategy_variants
         WHERE status = 'active' AND base_strategy = %s AND score IS NOT NULL
         ORDER BY score DESC LIMIT %s)
        UNION ALL
        (SELECT * FROM strategy_variants
         WHERE status = 'active' AND base_strategy = %s AND score IS NULL
         ORDER BY created_at DESC LIMIT %s)
    """, (strategy_id, top_n, strategy_id, fresh_n))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def cull_variants(strategy_id: str, keep_top: int = 30, keep_unevaluated: int = 20) -> int:
    """优存劣汰的"汰"。generation=0 种子永不退役(保底)。退役三类:
      1. 已评分活跃变体按 score DESC 只保留前 keep_top,其余退役;
      2. 已评分但 score<=0 的(0触发/净亏,永无价值)一律退役;
      3. 未评估(score IS NULL)只保留**最新** keep_unevaluated 个,更老的退役
         —— get_variants_for_eval 按 created_at DESC 取新生评估,老的未评估永远排不到、
         只堆成垃圾(实测最老 35 天仍 NULL);按同序保留新的、退役老的,零损失。
    返回退役总数。
    (修复 2026-07-16:原只汰"已评分 top_n 之外",从不碰未评分 → 1582 个未评估无限堆积;
     叠加 evolve_generation 无达标父代时提前 return、走不到本函数 → 部分策略只生不汰。)"""
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    # 1. 已评分:score DESC 保留前 keep_top,其余退役
    cur.execute("""
        UPDATE strategy_variants SET status = 'retired'
        WHERE id IN (
            SELECT id FROM strategy_variants
            WHERE status='active' AND base_strategy=%s AND score IS NOT NULL AND generation>0
            ORDER BY score DESC OFFSET %s)
    """, (strategy_id, keep_top))
    n += cur.rowcount
    # 2. 已评分但 score<=0 的垃圾一律退役(0触发/净亏,永无价值)
    cur.execute("""
        UPDATE strategy_variants SET status = 'retired'
        WHERE status='active' AND base_strategy=%s AND score IS NOT NULL AND score<=0 AND generation>0
    """, (strategy_id,))
    n += cur.rowcount
    # 3. 未评估:created_at DESC 保留最新 keep_unevaluated,更老的退役(它们永远排不到评估)
    cur.execute("""
        UPDATE strategy_variants SET status = 'retired'
        WHERE id IN (
            SELECT id FROM strategy_variants
            WHERE status='active' AND base_strategy=%s AND score IS NULL AND generation>0
            ORDER BY created_at DESC OFFSET %s)
    """, (strategy_id, keep_unevaluated))
    n += cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return n


def evolve_generation(strategy_id: str,
                      population_size: int = 30,
                      elites: int = 5,
                      mutants: int = 20,
                      mutation_strength: float = 0.25,
                      min_score: float = 20,
                      keep_top: int = 30) -> List[int]:
    """一代进化：从活跃变体中选优 → 变异 → 交叉 → 入库 → 淘汰

    修复(2026-06-12):
      - 父代只取"已评估且 score≥min_score"的变体(原来 min_score 形参被忽略,
        未评估的 NULL 分变体也被当父代变异,种群充满未验证的二代噪声)
      - 进化后调用 cull_variants 退役低分旧变体,控制种群规模
    返回新变体的 id 列表
    """
    active = get_active_variants(strategy_id, limit=population_size * 2)
    # 父代仅限已评估达标者
    evaluated = [v for v in active
                 if v.get('score') is not None and v['score'] >= min_score]

    if not evaluated:
        return []

    # 按 score 排序
    evaluated.sort(key=lambda x: x.get('score') or 0, reverse=True)
    evaluated = evaluated[:population_size]

    # 精英直接保留
    elite_variants = evaluated[:elites]
    # 父代池 = 精英 + 中游（用于交叉）
    parent_pool = evaluated[:max(elites * 3, 10)]

    new_ids = []

    # 变异：从精英和中游中各取一半变异
    mutation_candidates = elite_variants + evaluated[elites:elites + mutants]
    for parent in mutation_candidates:
        base_params = parent['params'] if isinstance(parent['params'], dict) else json.loads(parent['params'])
        new_params = mutate_params(strategy_id, base_params, mutation_strength)
        new_ids.append(_insert_variant(strategy_id, parent['strategy_cn'],
                                       parent['generation'] + 1, new_params,
                                       'active', parent['id']))

    # 交叉：随机两对父代
    if len(parent_pool) >= 2:
        for _ in range(mutants // 2):
            p1, p2 = random.sample(parent_pool, 2)
            params1 = p1['params'] if isinstance(p1['params'], dict) else json.loads(p1['params'])
            params2 = p2['params'] if isinstance(p2['params'], dict) else json.loads(p2['params'])
            child = crossover_params(coerce_params(strategy_id, params1),
                                     coerce_params(strategy_id, params2))
            gen = max(p1['generation'], p2['generation']) + 1
            pid = p1['id'] if (p1.get('score') or 0) >= (p2.get('score') or 0) else p2['id']
            new_ids.append(_insert_variant(strategy_id, p1['strategy_cn'],
                                           gen, child, 'active', pid))

    # 汰:控制种群规模
    try:
        cull_variants(strategy_id, keep_top=keep_top)
    except Exception:
        pass

    return new_ids


def _insert_variant(base_strategy: str, strategy_cn: str,
                    generation: int, params: Dict, status: str,
                    parent_id: int = None) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO strategy_variants (base_strategy, strategy_cn, generation, params, status, parent_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (base_strategy, params) DO UPDATE SET
            generation = EXCLUDED.generation,
            status = 'active',
            parent_id = COALESCE(strategy_variants.parent_id, EXCLUDED.parent_id)
        RETURNING id
    """, (base_strategy, strategy_cn, generation, json.dumps(params), status, parent_id))
    vid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return vid


# ══════════════════════════════════════════════════════════
#  三层进化策略分组(2026-06-12)
#    FIXED   — 经典核心,只小步微调(mutation_strength≈0.1)
#    DYNAMIC — 其余 InStock 策略,大步动态调整(≈0.3)
#    COMPOSED— 条件积木组合出的全新策略(结构进化+每日随机新血)
# ══════════════════════════════════════════════════════════

FIXED_STRATEGIES = {'turtle_trade', 'keep_increasing', 'enter', 'backtrace_ma250'}
COMPOSED_BASE = 'composed'


def ensure_composed_population(min_active: int = 12) -> int:
    """组合策略种群保底:活跃数不足 min_active 时随机补齐。返回新增数。"""
    from analysis.strategy_composer import random_genes, genes_cn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM strategy_variants WHERE status='active' AND base_strategy=%s",
                (COMPOSED_BASE,))
    n_active = cur.fetchone()[0] or 0
    cur.close()
    conn.close()

    created = 0
    for _ in range(max(0, min_active - n_active)):
        genes = random_genes()
        _insert_variant(COMPOSED_BASE, '🧪' + genes_cn(genes, max_len=28), 0,
                        {'genes': genes}, 'active')
        created += 1
    return created


def evolve_composed(mutants: int = 8, randoms: int = 4,
                    keep_top: int = 40, min_score: float = 25) -> List[int]:
    """组合策略进化:已评估高分父代 → 基因变异/交叉 + 每日注入随机新血 → 淘汰。
    返回新变体 id 列表。"""
    from analysis.strategy_composer import (
        random_genes, mutate_genes, crossover_genes, genes_cn,
    )
    # 种群保底
    try:
        ensure_composed_population()
    except Exception:
        pass

    active = get_active_variants(COMPOSED_BASE, limit=keep_top * 2)
    evaluated = [v for v in active
                 if v.get('score') is not None and v['score'] >= min_score]
    evaluated.sort(key=lambda x: x.get('score') or 0, reverse=True)

    new_ids = []

    def _genes_of(v):
        p = v['params'] if isinstance(v['params'], dict) else json.loads(v['params'])
        return p.get('genes') or []

    # 变异:对高分父代做基因扰动+结构增删换
    for parent in evaluated[:mutants]:
        genes = mutate_genes(_genes_of(parent))
        new_ids.append(_insert_variant(
            COMPOSED_BASE, '🧪' + genes_cn(genes, max_len=28),
            (parent.get('generation') or 0) + 1, {'genes': genes},
            'active', parent['id']))

    # 交叉:高分父代两两混合
    if len(evaluated) >= 2:
        for _ in range(max(1, mutants // 3)):
            p1, p2 = random.sample(evaluated[:max(6, mutants)], 2)
            genes = crossover_genes(_genes_of(p1), _genes_of(p2))
            gen = max(p1.get('generation') or 0, p2.get('generation') or 0) + 1
            new_ids.append(_insert_variant(
                COMPOSED_BASE, '🧪' + genes_cn(genes, max_len=28),
                gen, {'genes': genes}, 'active', p1['id']))

    # 随机新血:保持探索,防局部最优
    for _ in range(randoms):
        genes = random_genes()
        new_ids.append(_insert_variant(
            COMPOSED_BASE, '🧪' + genes_cn(genes, max_len=28), 0,
            {'genes': genes}, 'active'))

    try:
        cull_variants(COMPOSED_BASE, keep_top=keep_top)
    except Exception:
        pass
    return new_ids


def get_live_strategy_set(max_composed: int = 5, composed_min_score: float = 45,
                          require_holdout: bool = True,
                          holdout_min_ret: float = 0.0, holdout_min_trigger: int = 3,
                          auto_revert: bool = True) -> Dict[str, Any]:
    """给实盘选股用的"当前最优策略集":
      base: {策略id: 最优变体参数}(已评估的活跃变体里分数最高者,含默认种子)
      composed: [{'vid','cn','genes','score'}](达标的组合策略 TopN)
    样本外部署门(require_holdout=True):**进化出的变体须 holdout(样本外)触发≥N 且平均收益≥下限才上线**,
      否则该策略回退 generation=0 默认参数(默认种子永远可部署=安全基线)→ 杜绝部署过拟合变体。
    自动回退(auto_revert=True):若近 N 次组合级 A/B(evolution_ab)进化集持续跑输全默认集,
      整体回退全默认参数(安全网)。A/B 测量自身须传 auto_revert=False 取真实进化集,避免自指。
    失败返回空集,调用方回退默认参数。"""
    if auto_revert:
        try:
            if evolution_recently_underperforming():
                print('[genome] 进化近期 A/B 跑输默认集 → get_live_strategy_set 自动回退全默认参数')
                return {'base': _all_default_base(), 'composed': []}
        except Exception:
            pass
    out = {'base': {}, 'composed': []}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if require_holdout:
        # 优先选 holdout 验证通过的高分变体;无则回退 generation=0 默认(保证每策略总有可部署参数)
        cur.execute("""
            SELECT DISTINCT ON (base_strategy) base_strategy, params, score
            FROM strategy_variants
            WHERE status = 'active' AND score IS NOT NULL AND base_strategy != %s
              AND (generation = 0
                   OR (holdout_trigger >= %s AND holdout_avg_ret_pct >= %s))
            ORDER BY base_strategy,
                     (CASE WHEN holdout_trigger >= %s AND holdout_avg_ret_pct >= %s
                           THEN 1 ELSE 0 END) DESC,
                     score DESC
        """, (COMPOSED_BASE, holdout_min_trigger, holdout_min_ret,
              holdout_min_trigger, holdout_min_ret))
    else:
        cur.execute("""
            SELECT DISTINCT ON (base_strategy) base_strategy, params, score
            FROM strategy_variants
            WHERE status = 'active' AND score IS NOT NULL AND base_strategy != %s
            ORDER BY base_strategy, score DESC
        """, (COMPOSED_BASE,))
    for r in cur.fetchall():
        params = r['params'] if isinstance(r['params'], dict) else json.loads(r['params'])
        out['base'][r['base_strategy']] = coerce_params(r['base_strategy'], params)

    # 组合策略:require_holdout 时,排除"已评估且 holdout 收益不达标"的(未评估的 NULL 给探索宽限)
    cur.execute("""
        SELECT id, strategy_cn, params, score
        FROM strategy_variants
        WHERE status = 'active' AND base_strategy = %s
          AND score IS NOT NULL AND score >= %s
          AND (NOT %s OR holdout_avg_ret_pct IS NULL OR holdout_avg_ret_pct >= %s)
        ORDER BY score DESC LIMIT %s
    """, (COMPOSED_BASE, composed_min_score, require_holdout, holdout_min_ret, max_composed))
    for r in cur.fetchall():
        params = r['params'] if isinstance(r['params'], dict) else json.loads(r['params'])
        out['composed'].append({'vid': r['id'], 'cn': r['strategy_cn'],
                                'genes': params.get('genes') or [], 'score': r['score']})
    cur.close()
    conn.close()
    return out


def promote_variant(variant_id: int):
    """升级变体为'promoted'（胜率超过同策略其他变体 5%+）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE strategy_variants SET status = 'promoted' WHERE id = %s", (variant_id,))
    conn.commit()
    cur.close()
    conn.close()


# ══════════════════════════════════════════════════════════
#  进化效果闭环:A/B(进化集 vs 全默认集)度量 + 自动回退安全网
# ══════════════════════════════════════════════════════════

def default_strategy_combos() -> List[Tuple[str, Dict[str, Any]]]:
    """全默认(generation=0)策略组合 — A/B 的对照基线(= 没有进化时系统会用的参数)。
    只含 InStock 基础策略,不含 composed(组合策略是进化的产物,基线里没有)。"""
    return [(sid, coerce_params(sid, default_params(sid))) for sid in STRATEGY_PARAM_SPACE]


def _all_default_base() -> Dict[str, Dict[str, Any]]:
    """{sid: 默认参数} — auto_revert 触发时 get_live_strategy_set 的回退结果。"""
    return {sid: coerce_params(sid, default_params(sid)) for sid in STRATEGY_PARAM_SPACE}


# 进化适应度评估的固定基准样本(跨行业大/中盘,非按近期涨幅筛选)。
# 用途:替代 task_daily_backtest 原来的"昨日强势股 TOP20"——后者是被近期表现选出来的,
# 在其上回测"买强势"类策略 = 选择偏差,系统性高估所有动量类策略的胜率/收益,使适应度失真。
# 固定多元样本让变体适应度可比、无近期表现偏差。(仍有轻微幸存者偏差,但相比"昨日强势股"是
# 数量级改善;名单可调,理想态是 point-in-time 指数成分。)
EVOLUTION_FITNESS_UNIVERSE: Dict[str, str] = {
    # 消费/家电
    '600519': '贵州茅台', '000858': '五粮液', '000568': '泸州老窖', '603288': '海天味业',
    '600887': '伊利股份', '000333': '美的集团', '000651': '格力电器', '600690': '海尔智家',
    # 金融
    '600036': '招商银行', '601318': '中国平安', '600030': '中信证券', '601166': '兴业银行',
    '601398': '工商银行',
    # 医药
    '600276': '恒瑞医药', '300760': '迈瑞医疗', '603259': '药明康德', '000538': '云南白药',
    # 科技/电子/新能源车
    '002415': '海康威视', '002230': '科大讯飞', '300059': '东方财富', '002594': '比亚迪',
    '300750': '宁德时代', '002475': '立讯精密', '000725': '京东方A',
    # 电力/新能源/公用
    '601012': '隆基绿能', '600900': '长江电力', '601985': '中国核电',
    # 工业/材料/地产/能源
    '600031': '三一重工', '601899': '紫金矿业', '600028': '中国石化', '601857': '中国石油',
    '600585': '海螺水泥', '000002': '万科A', '600309': '万华化学',
    # 汽车/交运/通信/其他
    '600104': '上汽集团', '600009': '上海机场', '600050': '中国联通', '000063': '中兴通讯',
    '601888': '中国中免', '002304': '洋河股份',
}


def evolution_fitness_pool(holdings: Dict[str, str] = None) -> Dict[str, str]:
    """进化适应度评估股池 = 持仓 ∪ 固定多元基准样本(EVOLUTION_FITNESS_UNIVERSE)。
    持仓是你真实持有的(合理纳入);基准样本替代"昨日强势股"以消除近期表现选择偏差。
    返回 {code: name}(持仓优先保留其名称)。"""
    pool = dict(holdings or {})
    for code, name in EVOLUTION_FITNESS_UNIVERSE.items():
        pool.setdefault(code, name)
    return pool


def save_evolution_ab(period_start: str, period_end: str, pool_n: int,
                      evolved_n_strat: int,
                      evolved: Dict[str, Any], default: Dict[str, Any]) -> int:
    """保存一次 A/B 结果。evolved/default = portfolio_backtest 的 summary dict。
    excess_* = evolved - default(>0 表示进化集更优;max_dd 越大[越接近0]越好,故同样相减)。"""
    def _g(d, k):
        v = d.get(k)
        return float(v) if isinstance(v, (int, float)) else None

    ev_ret, df_ret = _g(evolved, 'total_return_pct'), _g(default, 'total_return_pct')
    excess = (round(ev_ret - df_ret, 2) if ev_ret is not None and df_ret is not None else None)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO evolution_ab (eval_date, period_start, period_end, pool_n, evolved_n_strat,
            evolved_return_pct, default_return_pct, excess_return_pct,
            evolved_cagr_pct, default_cagr_pct, evolved_sharpe, default_sharpe,
            evolved_max_dd_pct, default_max_dd_pct)
        VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (eval_date) DO UPDATE SET
            period_start=EXCLUDED.period_start, period_end=EXCLUDED.period_end,
            pool_n=EXCLUDED.pool_n, evolved_n_strat=EXCLUDED.evolved_n_strat,
            evolved_return_pct=EXCLUDED.evolved_return_pct, default_return_pct=EXCLUDED.default_return_pct,
            excess_return_pct=EXCLUDED.excess_return_pct,
            evolved_cagr_pct=EXCLUDED.evolved_cagr_pct, default_cagr_pct=EXCLUDED.default_cagr_pct,
            evolved_sharpe=EXCLUDED.evolved_sharpe, default_sharpe=EXCLUDED.default_sharpe,
            evolved_max_dd_pct=EXCLUDED.evolved_max_dd_pct, default_max_dd_pct=EXCLUDED.default_max_dd_pct
        RETURNING id
    """, (period_start, period_end, pool_n, evolved_n_strat,
          ev_ret, df_ret, excess,
          _g(evolved, 'cagr_pct'), _g(default, 'cagr_pct'),
          _g(evolved, 'sharpe'), _g(default, 'sharpe'),
          _g(evolved, 'max_dd_pct'), _g(default, 'max_dd_pct')))
    rid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return rid


def get_evolution_ab_history(limit: int = 30) -> List[Dict]:
    """取最近的 A/B 记录(新→旧)。表不存在/失败返回 []。"""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT eval_date, period_start, period_end, pool_n, evolved_n_strat,
                   evolved_return_pct, default_return_pct, excess_return_pct,
                   evolved_cagr_pct, default_cagr_pct, evolved_sharpe, default_sharpe,
                   evolved_max_dd_pct, default_max_dd_pct
            FROM evolution_ab ORDER BY eval_date DESC LIMIT %s
        """, (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception:
        return []


def _ab_excess_is_negative(history: List[Dict], min_records: int = 3) -> bool:
    """纯判定:近 history 条里 excess_return 均值<0 且样本≥min_records 才算"持续跑输"。
    样本不足→False(fail-safe:数据不够不轻易回退)。"""
    vals = [h['excess_return_pct'] for h in history
            if h.get('excess_return_pct') is not None]
    if len(vals) < min_records:
        return False
    return (sum(vals) / len(vals)) < 0


def evolution_recently_underperforming(lookback: int = 6, min_records: int = 3) -> bool:
    """近 lookback 次 A/B 进化集是否持续跑输默认集(供 get_live_strategy_set 自动回退)。
    带 1h 缓存避免每次选股都查库;缓存/查库失败一律 False(fail-safe,不影响现有行为)。"""
    try:
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    ckey = f'evolution_ab_underperform:{lookback}:{min_records}'
    if cache_get:
        hit = cache_get(ckey)
        if isinstance(hit, bool):
            return hit
    try:
        verdict = _ab_excess_is_negative(get_evolution_ab_history(lookback), min_records)
    except Exception:
        verdict = False
    if cache_set:
        try:
            cache_set(ckey, verdict, 3600)
        except Exception:
            pass
    return verdict


# ══════════════════════════════════════════════════════════
#  情报输出（供 AI 分析注入）
# ══════════════════════════════════════════════════════════

def get_strategy_intelligence(stock_code: str = None, days: int = 30) -> Dict[str, Any]:
    """获取策略情报 — 供 AI 选股分析注入

    返回：
      market: 全市场策略效能排行（按 score 降序）
      stock: 个股策略适配度（若提供 stock_code）
    """
    result = {'market': [], 'stock': []}

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    # 全市场排行：取最新一天所有策略评分
    cur.execute("""
        SELECT DISTINCT ON (strategy_id) strategy_id, score, win_rate_pct, avg_ret_pct,
               stock_pool_n, triggered_n, eval_date, market_regime
        FROM strategy_scores
        WHERE eval_date >= %s
        ORDER BY strategy_id, eval_date DESC
    """, (cutoff,))
    for row in cur.fetchall():
        result['market'].append(dict(row))
    result['market'].sort(key=lambda x: x.get('score') or 0, reverse=True)

    # 个股适配
    if stock_code:
        cur.execute("""
            SELECT strategy_id, win_rate_pct, avg_ret_pct, trigger_count, score
            FROM stock_strategy_affinity
            WHERE stock_code = %s AND last_eval_date >= %s
            ORDER BY score DESC
        """, (stock_code, cutoff))
        result['stock'] = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()
    return result


def format_intelligence_for_ai(intel: Dict[str, Any]) -> str:
    """把情报格式化成 AI prompt 可读文本"""
    lines = []

    # 全市场排行
    market = intel.get('market', [])
    if market:
        regime = next((m.get('market_regime') for m in market if m.get('market_regime')), None)
        head = '📊 **全市场策略效能排行**（近30天横截面回测'
        head += f',当前环境:{regime}）：' if regime else '）：'
        lines.append(head)
        for i, m in enumerate(market[:8]):
            tag = '🟢' if (m.get('score') or 0) >= 60 else ('🟡' if (m.get('score') or 0) >= 40 else '🔴')
            trig = m.get('triggered_n', 0) or 0
            pool = m.get('stock_pool_n', 1) or 1
            lines.append(
                f"  {tag} {m['strategy_id']}"
                f" | 评分{m.get('score', 0):.0f}"
                f" | 胜率{m.get('win_rate_pct', 0):.0f}%"
                f" | 均收益{m.get('avg_ret_pct', 0):+.1f}%"
                f" | 触发{trig}/{pool}"
            )

    # 个股适配
    stock = intel.get('stock', [])
    if stock:
        lines.append(f'\n🎯 **该股历史策略适配度**：')
        for s in stock[:5]:
            tag = '🟢' if (s.get('score') or 0) >= 60 else ('🔴' if (s.get('score') or 0) < 40 else '🟡')
            lines.append(
                f"  {tag} {s['strategy_id']}"
                f" | 评分{s.get('score', 0):.0f}"
                f" | 胜率{s.get('win_rate_pct', 0):.0f}%"
                f" | 均收益{s.get('avg_ret_pct', 0):+.1f}%"
                f" | 触发{s.get('trigger_count', 0)}次"
            )
        # 推荐/不推荐
        good = [s for s in stock if (s.get('score') or 0) >= 60]
        bad = [s for s in stock if (s.get('score') or 0) <= 30]
        if good:
            lines.append(f'  ✅ 推荐策略: {", ".join(s["strategy_id"] for s in good[:3])}')
        if bad:
            lines.append(f'  ❌ 不推荐: {", ".join(s["strategy_id"] for s in bad[:3])}')

    if not lines:
        lines.append('(尚无策略情报数据，等待回测积累)')

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════
#  进化日报（QQ 推送用）
# ══════════════════════════════════════════════════════════

def build_evolution_report() -> str:
    """生成策略进化日报"""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    today = datetime.now().strftime('%Y-%m-%d')

    # 今日评分 TOP 5
    cur.execute("""
        SELECT strategy_id, score, win_rate_pct, avg_ret_pct, stock_pool_n, triggered_n
        FROM strategy_scores
        WHERE eval_date = %s
        ORDER BY score DESC
        LIMIT 8
    """, (today,))
    top_today = [dict(r) for r in cur.fetchall()]

    # 今日新增变体
    cur.execute("""
        SELECT base_strategy, strategy_cn, generation, score, win_rate_pct, avg_ret_pct
        FROM strategy_variants
        WHERE created_at::date = %s AND generation > 0
        ORDER BY score DESC NULLS LAST
    """, (today,))
    new_variants = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    lines = [f'🧬 策略进化日报 · {today}']

    if top_today:
        lines.append('\n📊 今日全市场策略效能：')
        for t in top_today:
            tag = '🟢' if (t.get('score') or 0) >= 60 else ('🟡' if (t.get('score') or 0) >= 40 else '🔴')
            trig = t.get('triggered_n', 0) or 0
            pool = t.get('stock_pool_n', 1) or 1
            lines.append(
                f"  {tag} {strategy_cn(t['strategy_id'])}"
                f" 评分{t.get('score', 0):.0f}"
                f" 胜率{t.get('win_rate_pct', 0):.0f}%"
                f" 均收益{t.get('avg_ret_pct', 0):+.1f}%"
                f" (触发{trig}/{pool}只)"
            )

    if new_variants:
        lines.append(f'\n🧪 今日新生变体（{len(new_variants)}个）：')
        for nv in new_variants:
            score = nv.get('score') or 0
            tag = '✅' if score >= 50 else '⏳'
            lines.append(
                f"  {tag} {nv['strategy_cn'] or nv['base_strategy']} "
                f"gen{nv['generation']} "
                f"{'评分'+str(score) if score else '待评估'}"
            )
    else:
        lines.append('\n🧪 今日无新生变体（样本不足或未触发进化）')

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════
#  CLI 自检
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    print('=== 策略基因组引擎自检 ===')
    print(f'策略参数空间: {len(STRATEGY_PARAM_SPACE)} 套')

    init_genome_tables()
    print('✓ 表已就绪')

    n = seed_default_variants()
    print(f'✓ 默认种子: {n} 套')

    # 模拟一次变异
    for sid in ['enter', 'keep_increasing']:
        base = default_params(sid)
        mutated = mutate_params(sid, base)
        print(f'  {sid} 原参数: {base}')
        print(f'  {sid} 变异: {mutated}')
        score_shallow = compute_strategy_score(65, 8.2, 15, 30, 28, max_dd=-5.0)
        score_deep = compute_strategy_score(65, 8.2, 15, 30, 28, max_dd=-18.0)
        print(f'  示例评分(胜率65%, 均收益8.2%, 15/30触发, 28样本) '
              f'浅回撤-5%: {score_shallow} | 深回撤-18%: {score_deep}（风险项已生效）')

    # 情报
    intel = get_strategy_intelligence()
    print('\n情报输出:')
    print(format_intelligence_for_ai(intel))
