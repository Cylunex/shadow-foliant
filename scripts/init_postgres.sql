-- =============================================================================
-- AI股票分析系统 PostgreSQL 建表脚本
-- 用法：psql -h <PG_HOST> -p <PG_PORT> -U <PG_USER> -d aiagents_stock -f init_postgres.sql
-- 说明：从 SQLite 版本 schema 反推得到，列类型已按 PG 习惯调整
--
-- ⚠️ 标准化优化(2026-06-06)：本文件建基础表后，再跑一次幂等迁移以应用
--    枚举(job_status/signal/notif_type)、rating/confidence 中文 CHECK、价格列 NUMERIC、
--    updated_at 触发器、补 FK 索引：
--        python scripts/migrate_optimize_20260606.py
--    分类值在应用层由 core/enums.py 的 normalize_* 归一(LLM 出英文 → 中文规范值)。
-- =============================================================================

-- ---------- analysis_records (来自 database.py) ----------
CREATE TABLE IF NOT EXISTS analysis_records (
    id                BIGSERIAL PRIMARY KEY,
    symbol            TEXT NOT NULL,
    stock_name        TEXT,
    analysis_date     TIMESTAMPTZ NOT NULL,
    period            TEXT NOT NULL,
    stock_info        JSONB,
    agents_results    JSONB,
    discussion_result JSONB,
    final_decision    JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_analysis_symbol     ON analysis_records (symbol);
CREATE INDEX IF NOT EXISTS idx_analysis_created_at ON analysis_records (created_at DESC);


-- ---------- portfolio_stocks / portfolio_analysis_history (来自 portfolio_db.py) ----------
CREATE TABLE IF NOT EXISTS portfolio_stocks (
    id           BIGSERIAL PRIMARY KEY,
    code         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    cost_price   DOUBLE PRECISION,
    quantity     INTEGER,
    note         TEXT,
    auto_monitor BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 持仓变动历史 — 每次 add/update/delete 自动记录，用于报表和 AI 诊断
CREATE TABLE IF NOT EXISTS portfolio_changes (
    id            BIGSERIAL PRIMARY KEY,
    code          TEXT NOT NULL,
    name          TEXT,
    change_type   TEXT NOT NULL,   -- add / update / delete / bulk_import
    old_data      JSONB,           -- 变动前快照（add 时为 NULL）
    new_data      JSONB,           -- 变动后快照（delete 时为 NULL）
    cost_price    DOUBLE PRECISION,
    quantity      INTEGER,
    delta_qty     INTEGER,         -- 数量增减（正=买入，负=卖出，NULL=非交易变动）
    source        TEXT,            -- 来源：ui_manual / bulk_import / ai_auto / api 等
    note          TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pc_code_time  ON portfolio_changes (code, changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_pc_type_time  ON portfolio_changes (change_type, changed_at DESC);


CREATE TABLE IF NOT EXISTS portfolio_analysis_history (
    id                 BIGSERIAL PRIMARY KEY,
    portfolio_stock_id BIGINT NOT NULL REFERENCES portfolio_stocks(id) ON DELETE CASCADE,
    analysis_time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rating             TEXT,
    confidence         DOUBLE PRECISION,
    current_price      DOUBLE PRECISION,
    target_price       DOUBLE PRECISION,
    entry_min          DOUBLE PRECISION,
    entry_max          DOUBLE PRECISION,
    take_profit        DOUBLE PRECISION,
    stop_loss          DOUBLE PRECISION,
    summary            TEXT
);
CREATE INDEX IF NOT EXISTS idx_portfolio_analysis_stock_id ON portfolio_analysis_history (portfolio_stock_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_analysis_time    ON portfolio_analysis_history (analysis_time DESC);


-- ---------- 智瞰龙虎 (来自 longhubang_db.py) ----------
CREATE TABLE IF NOT EXISTS longhubang_records (
    id          BIGSERIAL PRIMARY KEY,
    date        DATE NOT NULL,
    stock_code  TEXT NOT NULL,
    stock_name  TEXT,
    youzi_name  TEXT,
    yingye_bu   TEXT,
    list_type   TEXT,
    buy_amount  DOUBLE PRECISION,
    sell_amount DOUBLE PRECISION,
    net_inflow  DOUBLE PRECISION,
    concepts    TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (date, stock_code, youzi_name, yingye_bu)
);
CREATE INDEX IF NOT EXISTS idx_lhb_date       ON longhubang_records (date);
CREATE INDEX IF NOT EXISTS idx_lhb_stock_code ON longhubang_records (stock_code);
CREATE INDEX IF NOT EXISTS idx_lhb_youzi_name ON longhubang_records (youzi_name);
CREATE INDEX IF NOT EXISTS idx_lhb_net_inflow ON longhubang_records (net_inflow);

CREATE TABLE IF NOT EXISTS longhubang_analysis (
    id                  BIGSERIAL PRIMARY KEY,
    analysis_date       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_date_range     TEXT,
    analysis_content    TEXT,
    recommended_stocks  JSONB,
    summary             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stock_tracking (
    id                BIGSERIAL PRIMARY KEY,
    analysis_id       BIGINT REFERENCES longhubang_analysis(id) ON DELETE CASCADE,
    stock_code        TEXT NOT NULL,
    stock_name        TEXT,
    recommended_date  DATE,
    recommended_price DOUBLE PRECISION,
    target_price      DOUBLE PRECISION,
    stop_loss_price   DOUBLE PRECISION,
    current_price     DOUBLE PRECISION,
    profit_loss_pct   DOUBLE PRECISION,
    status            TEXT,
    notes             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tracking_analysis_id ON stock_tracking (analysis_id);
CREATE INDEX IF NOT EXISTS idx_tracking_stock_code  ON stock_tracking (stock_code);


-- =============================================================================
-- jobs_hub 后台任务（来自 jobs_snapshots.db）
-- =============================================================================
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    symbol        TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    indicators    JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_indicator_symbol  ON indicator_snapshots (symbol);
CREATE INDEX IF NOT EXISTS idx_indicator_date    ON indicator_snapshots (snapshot_date DESC);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id            BIGSERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL UNIQUE,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_runs (
    id          BIGSERIAL PRIMARY KEY,
    job_name    TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL,
    error       TEXT
);

-- Agent/Web 手动触发任务：异步提交后可跨连接按 run_id 查询状态。
CREATE TABLE IF NOT EXISTS manual_task_runs (
    run_id          TEXT PRIMARY KEY,
    task_name       TEXT NOT NULL,
    requested_by    TEXT NOT NULL,
    idempotency_key TEXT,
    status          TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    result_json     TEXT,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 2,
    worker_id       TEXT,
    heartbeat_at    TEXT,
    UNIQUE(requested_by, task_name, idempotency_key)
);
-- 兼容已由上一版创建的表；CREATE TABLE IF NOT EXISTS 不会补列。
ALTER TABLE manual_task_runs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE manual_task_runs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 2;
ALTER TABLE manual_task_runs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE manual_task_runs ADD COLUMN IF NOT EXISTS heartbeat_at TEXT;
CREATE INDEX IF NOT EXISTS idx_manual_task_runs_recent
    ON manual_task_runs(requested_at);
CREATE INDEX IF NOT EXISTS idx_manual_task_runs_task
    ON manual_task_runs(task_name, requested_at);
CREATE INDEX IF NOT EXISTS idx_manual_task_runs_queue
    ON manual_task_runs(status, requested_at);
CREATE INDEX IF NOT EXISTS idx_job_runs_name_time ON job_runs (job_name, started_at DESC);


-- =============================================================================
-- AI 盯盘（来自 smart_monitor.db） — AI 决策、交易、持仓
-- =============================================================================
CREATE TABLE IF NOT EXISTS monitor_tasks (
    id              BIGSERIAL PRIMARY KEY,
    task_name       TEXT,
    stock_code      TEXT NOT NULL,
    stock_name      TEXT,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    check_interval  INTEGER DEFAULT 60,
    config          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id                BIGSERIAL PRIMARY KEY,
    stock_code        TEXT NOT NULL,
    stock_name        TEXT,
    decision_time     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trading_session   TEXT,
    action            TEXT,
    confidence        DOUBLE PRECISION,
    reason            TEXT,
    indicators        JSONB,
    raw_response      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_code_time ON ai_decisions (stock_code, decision_time DESC);

CREATE TABLE IF NOT EXISTS smart_monitor_notifications (
    id           BIGSERIAL PRIMARY KEY,
    stock_code   TEXT NOT NULL,
    notify_type  TEXT,
    notify_target TEXT,
    subject      TEXT,
    content      TEXT,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    success      BOOLEAN
);

-- trade_records:成交流水 + 持仓变动统一时间线(2026-06 合并 portfolio_changes;2026-07-17 补齐
-- DDL 与代码对齐 —— 生产库是手工 ALTER 过的,新库缺列会让 _log_change 静默失败、bulk_import
-- 共享事务 aborted、"自动变动记录"名存实亡)
CREATE TABLE IF NOT EXISTS trade_records (
    id             BIGSERIAL PRIMARY KEY,
    stock_code     TEXT NOT NULL,
    stock_name     TEXT,
    trade_type     TEXT,               -- 买入/卖出(成交行) 或 新增/调整/删除(变动行)
    quantity       INTEGER,
    price          DOUBLE PRECISION,
    amount         DOUBLE PRECISION,
    pos_quantity   INTEGER,            -- 变更后持仓数量快照
    pos_cost_price DOUBLE PRECISION,   -- 变更后持仓成本快照
    delta_qty      INTEGER,            -- 本次数量增减(加仓+/减仓-)
    source         TEXT,               -- ui_manual/bulk_import/import_trades/ai_auto/api...
    note           TEXT,
    commission     DOUBLE PRECISION,   -- 佣金
    tax            DOUBLE PRECISION,   -- 印花税
    profit_loss    DOUBLE PRECISION,   -- 卖出已实现盈亏
    order_id       TEXT,
    change_id      BIGINT,
    trade_time     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extra          JSONB,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_records_code_time ON trade_records (stock_code, trade_time DESC);
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS broker_execution_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS account_ref TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS import_batch_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS external_fingerprint TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS position_effect TEXT NOT NULL DEFAULT 'legacy_unverified';
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS created_by_shadow_user_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS selection_run_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS nomination_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS strategy_id TEXT;
ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS decision_signal_id BIGINT;
CREATE INDEX IF NOT EXISTS idx_trade_records_selection_origin
    ON trade_records(selection_run_id, nomination_id, trade_time DESC)
    WHERE selection_run_id IS NOT NULL OR nomination_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_execution
    ON trade_records(source, account_ref, broker_execution_id)
    WHERE broker_execution_id IS NOT NULL AND broker_execution_id <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_records_fingerprint
    ON trade_records(external_fingerprint)
    WHERE external_fingerprint IS NOT NULL AND external_fingerprint <> '';

CREATE TABLE IF NOT EXISTS trade_import_batches (
    batch_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    update_position BOOLEAN NOT NULL,
    preview_hash TEXT NOT NULL,
    position_watermark TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    abandoned_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trade_import_batches_actor
    ON trade_import_batches(actor_id, created_at DESC);
CREATE TABLE IF NOT EXISTS trade_import_rows (
    batch_id TEXT NOT NULL REFERENCES trade_import_batches(batch_id),
    row_number INTEGER NOT NULL,
    external_fingerprint TEXT NOT NULL,
    normalized_payload JSONB NOT NULL,
    validation_status TEXT NOT NULL,
    error_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    trade_record_id BIGINT REFERENCES trade_records(id),
    PRIMARY KEY(batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS position_monitor (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    TEXT NOT NULL UNIQUE,
    stock_name    TEXT,
    quantity      INTEGER,
    cost_price    DOUBLE PRECISION,
    current_price DOUBLE PRECISION,
    pnl_pct       DOUBLE PRECISION,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS smart_monitor_logs (
    id          BIGSERIAL PRIMARY KEY,
    log_level   TEXT,
    module      TEXT,
    message     TEXT,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- 实时监测（来自 stock_monitor.db）
-- =============================================================================
CREATE TABLE IF NOT EXISTS monitored_stocks (
    id                    BIGSERIAL PRIMARY KEY,
    symbol                TEXT NOT NULL UNIQUE,
    name                  TEXT,
    rating                TEXT,
    entry_range           JSONB,
    take_profit           DOUBLE PRECISION,
    stop_loss             DOUBLE PRECISION,
    check_interval        INTEGER DEFAULT 60,
    notification_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    trading_hours_only    BOOLEAN NOT NULL DEFAULT TRUE,
    quant_enabled         BOOLEAN NOT NULL DEFAULT FALSE,
    quant_config          JSONB,
    current_price         DOUBLE PRECISION,
    last_checked          TIMESTAMPTZ,
    last_price            DOUBLE PRECISION,
    last_check_at         TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS current_price DOUBLE PRECISION;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS last_checked TIMESTAMPTZ;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS trading_hours_only BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS quant_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS quant_config JSONB;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS last_price DOUBLE PRECISION;
ALTER TABLE monitored_stocks ADD COLUMN IF NOT EXISTS last_check_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS monitor_notifications (
    id            BIGSERIAL PRIMARY KEY,
    stock_id      BIGINT REFERENCES monitored_stocks(id) ON DELETE CASCADE,
    type          TEXT,
    message       TEXT,
    triggered_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent          BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_monitor_notif_stock ON monitor_notifications (stock_id);

CREATE TABLE IF NOT EXISTS price_history (
    id         BIGSERIAL PRIMARY KEY,
    stock_id   BIGINT REFERENCES monitored_stocks(id) ON DELETE CASCADE,
    price      DOUBLE PRECISION,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_price_history_stock_ts ON price_history (stock_id, "timestamp" DESC);


-- =============================================================================
-- 智策板块（来自 sector_strategy.db） — 分析报告（不存原始数据缓存）
-- =============================================================================
CREATE TABLE IF NOT EXISTS sector_analysis_reports (
    id                    BIGSERIAL PRIMARY KEY,
    analysis_date         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_date_range       TEXT,
    analysis_content      TEXT,
    recommended_sectors   JSONB,
    summary               TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sector_tracking (
    id                BIGSERIAL PRIMARY KEY,
    analysis_id       BIGINT REFERENCES sector_analysis_reports(id) ON DELETE CASCADE,
    sector_code       TEXT NOT NULL,
    sector_name       TEXT,
    recommended_date  DATE,
    notes             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- 新闻流量（来自 news_flow.db） — AI 分析结果 + 预警 + 调度日志
-- 不迁移：platform_news (8000+ 行原始数据) / stock_related_news / hot_topics
-- =============================================================================
CREATE TABLE IF NOT EXISTS flow_snapshots (
    id               BIGSERIAL PRIMARY KEY,
    fetch_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_platforms  INTEGER,
    success_count    INTEGER,
    total_score      INTEGER,
    snapshot_data    JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()  -- 对齐 SQLite schema
);
CREATE INDEX IF NOT EXISTS idx_flow_snapshots_time ON flow_snapshots (fetch_time DESC);

CREATE TABLE IF NOT EXISTS ai_analysis (
    id                  BIGSERIAL PRIMARY KEY,
    snapshot_id         BIGINT REFERENCES flow_snapshots(id) ON DELETE SET NULL,
    affected_sectors    JSONB,
    recommended_stocks  JSONB,
    risk_level          TEXT,
    analysis_content    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS flow_alerts (
    id              BIGSERIAL PRIMARY KEY,
    alert_type      TEXT,
    alert_level     TEXT,
    title           TEXT,
    content         TEXT,
    related_topics  JSONB,
    notified        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_flow_alerts_time ON flow_alerts (created_at DESC);

CREATE TABLE IF NOT EXISTS sentiment_records (
    id                BIGSERIAL PRIMARY KEY,
    snapshot_id       BIGINT REFERENCES flow_snapshots(id) ON DELETE SET NULL,
    sentiment_index   INTEGER,
    sentiment_class   TEXT,
    flow_stage        TEXT,
    details           JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scheduler_logs (
    id            BIGSERIAL PRIMARY KEY,
    task_name     TEXT,
    task_type     TEXT,
    status        TEXT,
    message       TEXT,
    duration      DOUBLE PRECISION,
    snapshot_id   BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- 主力选股批量分析历史（来自 main_force_batch.db）
-- =============================================================================
CREATE TABLE IF NOT EXISTS batch_analysis_history (
    id              BIGSERIAL PRIMARY KEY,
    analysis_date   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    batch_count     INTEGER,
    analysis_mode   TEXT,
    success_count   INTEGER,
    failed_count    INTEGER,
    results         JSONB,
    notes           TEXT
);


-- ---------- user_strategy_config (用户专属策略参数 KV 存储，来自 user_strategy_config.py) ----------
CREATE TABLE IF NOT EXISTS user_strategy_config (
    id          BIGSERIAL PRIMARY KEY,
    key         TEXT NOT NULL UNIQUE,
    value_json  TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ---------- automation_switches (自动化任务开关，来自 automation_config.py) ----------
-- 所有定时/工作流任务默认关闭；用户在 Admin UI 一键开启
CREATE TABLE IF NOT EXISTS automation_switches (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note        TEXT
);


-- ---------- ai_recommendations (AI 推荐股票后台监控，来自 ai_recommendation_monitor.py) ----------
-- AI 任意分析输出"推荐买入/目标价"时入库 → 后台拉实时价对比 → 触发后通知（闭环用户体验）
CREATE TABLE IF NOT EXISTS ai_recommendations (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL,
    name            TEXT,
    source          TEXT,                       -- overnight_strategy / longhubang_analyst / etc
    rating          TEXT,                       -- strong_buy / buy / hold
    confidence      TEXT,                       -- 高 / 中 / 低
    target_price    DOUBLE PRECISION,
    entry_low       DOUBLE PRECISION,
    entry_high      DOUBLE PRECISION,
    take_profit     DOUBLE PRECISION,
    stop_loss       DOUBLE PRECISION,
    reason          TEXT,
    is_monitored    BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    hit_target_at   TIMESTAMPTZ,
    hit_stop_at     TIMESTAMPTZ,
    recommended_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ref_price       DOUBLE PRECISION,
    last_price      DOUBLE PRECISION,
    last_price_at   TIMESTAMPTZ,
    realized_pnl_pct DOUBLE PRECISION,
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS ref_price DOUBLE PRECISION;
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS last_price DOUBLE PRECISION;
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS last_price_at TIMESTAMPTZ;
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS realized_pnl_pct DOUBLE PRECISION;
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;
ALTER TABLE ai_recommendations ADD COLUMN IF NOT EXISTS close_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_air_symbol_time ON ai_recommendations (symbol, recommended_at DESC);
CREATE INDEX IF NOT EXISTS idx_air_active ON ai_recommendations (is_active, is_monitored);


-- ---------- prompt_templates (Prompt 模板 CRUD，来自 prompt_manager.py) ----------
-- 让 prompt 从代码硬编码解耦，支持运行时增改 + 按 scene 默认模板
CREATE TABLE IF NOT EXISTS prompt_templates (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    agent_type   TEXT,
    scene        TEXT,
    content      TEXT NOT NULL,
    description  TEXT,
    version      INTEGER NOT NULL DEFAULT 1,
    is_default   BOOLEAN NOT NULL DEFAULT FALSE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pt_scene ON prompt_templates (scene, is_default);


-- ---------- northbound_flow_daily (北向资金本地自缓存，来自 northbound_cache.py) ----------
-- eastmoney 全系北向数据 2024-08 起断供（净买额返回 0/NaN）；
-- 本表由 jobs_hub 每日 15:40 task_northbound_flow_refresh 从同花顺 hsgtApi 追加。
-- hgt_yi / sgt_yi 单位：亿元；net_total = hgt_yi + sgt_yi。
CREATE TABLE IF NOT EXISTS northbound_flow_daily (
    id          BIGSERIAL PRIMARY KEY,
    trade_date  DATE NOT NULL UNIQUE,
    hgt_yi      DOUBLE PRECISION,
    sgt_yi      DOUBLE PRECISION,
    net_total   DOUBLE PRECISION,
    source      TEXT DEFAULT 'hexin',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_nb_date_desc ON northbound_flow_daily (trade_date DESC);


-- ---------- local point-in-time research warehouse ----------
CREATE TABLE IF NOT EXISTS research_sync_runs (
    run_id TEXT PRIMARY KEY, provider TEXT NOT NULL, capability TEXT NOT NULL,
    as_of TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS research_source_runtime_state (
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    last_success_at TEXT,
    last_failure_at TEXT,
    last_error_category TEXT,
    cooldown_until TEXT,
    freshness_as_of TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, endpoint)
);
CREATE TABLE IF NOT EXISTS research_securities (
    symbol TEXT PRIMARY KEY, ts_code TEXT NOT NULL, name TEXT, exchange TEXT,
    market TEXT, industry TEXT, list_status TEXT, list_date TEXT, delist_date TEXT,
    is_hs TEXT, as_of TEXT NOT NULL, provider TEXT NOT NULL, origin TEXT NOT NULL,
    effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_daily_bars (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, adjustment TEXT NOT NULL,
    open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION,
    close DOUBLE PRECISION, volume DOUBLE PRECISION, amount DOUBLE PRECISION,
    turnover_rate DOUBLE PRECISION, is_paused INTEGER, is_st INTEGER,
    provider TEXT NOT NULL, origin TEXT NOT NULL, effective_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, unit TEXT NOT NULL, schema_version TEXT NOT NULL,
    quality_status TEXT NOT NULL, PRIMARY KEY(symbol, trade_date, adjustment)
);
CREATE INDEX IF NOT EXISTS idx_research_bars_date ON research_daily_bars(trade_date);
CREATE TABLE IF NOT EXISTS research_valuations (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, market_cap DOUBLE PRECISION,
    circulating_market_cap DOUBLE PRECISION, turnover_ratio DOUBLE PRECISION,
    pe_ttm DOUBLE PRECISION, pe_lyr DOUBLE PRECISION, pb DOUBLE PRECISION,
    ps DOUBLE PRECISION, pcf DOUBLE PRECISION, dividend_yield DOUBLE PRECISION,
    provider TEXT NOT NULL,
    origin TEXT NOT NULL, effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(symbol, trade_date)
);
CREATE TABLE IF NOT EXISTS research_fund_flow_daily (
    symbol TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT,
    close DOUBLE PRECISION, change_pct DOUBLE PRECISION,
    main_net_inflow DOUBLE PRECISION, main_net_inflow_ratio DOUBLE PRECISION,
    provider TEXT NOT NULL, origin TEXT NOT NULL, effective_at TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, schema_version TEXT NOT NULL,
    quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_research_fund_flow_date
    ON research_fund_flow_daily(trade_date, main_net_inflow DESC);
CREATE TABLE IF NOT EXISTS research_financial_pit (
    table_name TEXT NOT NULL, symbol TEXT NOT NULL, as_of TEXT NOT NULL,
    stat_date TEXT, pub_date TEXT, provider TEXT NOT NULL, origin TEXT NOT NULL,
    effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(table_name, symbol, as_of)
);
CREATE INDEX IF NOT EXISTS idx_research_finance_asof ON research_financial_pit(as_of, table_name);
CREATE TABLE IF NOT EXISTS research_events (
    event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
    event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
    direction DOUBLE PRECISION NOT NULL, confidence DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL, official INTEGER NOT NULL DEFAULT 0,
    title TEXT, payload TEXT NOT NULL, retrieved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_events_date ON research_events(event_date, symbol);
CREATE TABLE IF NOT EXISTS selection_runs (
    run_id TEXT PRIMARY KEY, selection_date TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, primary_source TEXT NOT NULL,
    universe_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL,
    final_count INTEGER NOT NULL, coverage DOUBLE PRECISION NOT NULL,
    reference_source TEXT, comparison TEXT NOT NULL, metadata TEXT NOT NULL,
    publication_status TEXT NOT NULL DEFAULT 'unpublished', published_at TEXT,
    supersedes_run_id TEXT
);
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS publication_status TEXT NOT NULL DEFAULT 'unpublished';
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS published_at TEXT;
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS supersedes_run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_selection_runs_date ON selection_runs(selection_date, created_at);
CREATE TABLE IF NOT EXISTS selection_candidates (
    run_id TEXT NOT NULL, symbol TEXT NOT NULL, candidate_kind TEXT NOT NULL,
    rank_pos INTEGER, total_score DOUBLE PRECISION, fundamental_score DOUBLE PRECISION,
    technical_60_score DOUBLE PRECISION, industry_score DOUBLE PRECISION,
    quality_score DOUBLE PRECISION, correction_120 DOUBLE PRECISION,
    correction_250 DOUBLE PRECISION, event_correction DOUBLE PRECISION,
    data_coverage DOUBLE PRECISION, state TEXT, industry TEXT,
    reasons TEXT NOT NULL, source_labels TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(run_id, symbol, candidate_kind)
);

-- Reproducible research schema (V4).  Canonical tables remain fast read models;
-- observations, manifests and artifacts are append-only audit inputs/outputs.
CREATE TABLE IF NOT EXISTS research_schema_migrations (
    version TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_trade_calendar (
    trade_date TEXT PRIMARY KEY, provider TEXT NOT NULL, retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_trade_calendar_evidence (
    trade_date TEXT NOT NULL, provider TEXT NOT NULL, is_open INTEGER NOT NULL,
    retrieved_at TEXT NOT NULL, PRIMARY KEY(trade_date, provider)
);
CREATE TABLE IF NOT EXISTS research_calendar_fetch_runs (
    fetch_id TEXT PRIMARY KEY, provider TEXT NOT NULL, range_start TEXT NOT NULL,
    range_end TEXT NOT NULL, retrieved_at TEXT NOT NULL, row_count INTEGER NOT NULL,
    open_count INTEGER NOT NULL, closed_count INTEGER NOT NULL,
    quality_status TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_master_snapshot_runs (
    snapshot_id TEXT PRIMARY KEY, snapshot_date TEXT NOT NULL, provider TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, observed_count INTEGER NOT NULL,
    expected_min_count INTEGER NOT NULL, exchange_counts TEXT NOT NULL,
    previous_snapshot_id TEXT, quality_status TEXT NOT NULL, published_at TEXT,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_security_master_rows (
    snapshot_id TEXT NOT NULL, symbol TEXT NOT NULL, ts_code TEXT NOT NULL,
    name TEXT, exchange TEXT, market TEXT, industry TEXT, list_status TEXT,
    list_date TEXT, delist_date TEXT, is_hs TEXT, provider TEXT NOT NULL,
    origin TEXT NOT NULL, effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, symbol)
);
CREATE TABLE IF NOT EXISTS research_dataset_batches (
    dataset_id TEXT PRIMARY KEY, capability TEXT NOT NULL, provider TEXT NOT NULL,
    effective_as_of TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL,
    row_count INTEGER NOT NULL, payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_dataset_publications (
    capability TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    effective_as_of TEXT,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_dataset_publication_history (
    capability TEXT NOT NULL,
    generation INTEGER NOT NULL,
    dataset_id TEXT NOT NULL,
    effective_as_of TEXT,
    published_at TEXT NOT NULL,
    PRIMARY KEY(capability,generation)
);
CREATE TABLE IF NOT EXISTS research_market_observations (
    observation_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL, adjustment TEXT NOT NULL, provider TEXT NOT NULL,
    retrieved_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_valuation_observations (
    observation_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, symbol TEXT NOT NULL,
    requested_as_of TEXT NOT NULL, provider_effective_as_of TEXT NOT NULL,
    provider TEXT NOT NULL, retrieved_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_security_snapshots (
    snapshot_date TEXT NOT NULL, symbol TEXT NOT NULL, ts_code TEXT NOT NULL,
    name TEXT, exchange TEXT, market TEXT, industry TEXT, list_status TEXT,
    list_date TEXT, delist_date TEXT, is_hs TEXT, provider TEXT NOT NULL,
    origin TEXT NOT NULL, effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(snapshot_date, symbol)
);
CREATE TABLE IF NOT EXISTS research_financial_facts (
    table_name TEXT NOT NULL, symbol TEXT NOT NULL, stat_date TEXT NOT NULL,
    pub_date TEXT NOT NULL, revision_no TEXT NOT NULL, provider TEXT NOT NULL,
    first_seen_as_of TEXT NOT NULL, first_seen_at TEXT, origin TEXT NOT NULL,
    effective_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    schema_version TEXT NOT NULL, quality_status TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(table_name, symbol, stat_date, pub_date, revision_no, provider)
);
CREATE TABLE IF NOT EXISTS research_event_records (
    event_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
    event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
    direction DOUBLE PRECISION NOT NULL, confidence DOUBLE PRECISION NOT NULL,
    materiality DOUBLE PRECISION NOT NULL, surprise DOUBLE PRECISION NOT NULL,
    novelty DOUBLE PRECISION NOT NULL, source_family TEXT NOT NULL,
    source_origin TEXT NOT NULL, document_id TEXT NOT NULL,
    event_cluster_id TEXT NOT NULL, confirmation_status TEXT NOT NULL,
    entity_impact TEXT NOT NULL, official INTEGER NOT NULL DEFAULT 0, title TEXT,
    original_values TEXT NOT NULL, normalized_values TEXT NOT NULL,
    payload TEXT NOT NULL, retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_event_revisions (
    revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    supersedes_revision_id TEXT, symbol TEXT NOT NULL, event_type TEXT NOT NULL,
    event_date TEXT NOT NULL, effective_at TEXT NOT NULL,
    direction DOUBLE PRECISION NOT NULL, confidence DOUBLE PRECISION NOT NULL,
    materiality DOUBLE PRECISION NOT NULL, surprise DOUBLE PRECISION NOT NULL,
    novelty DOUBLE PRECISION NOT NULL, source_family TEXT NOT NULL,
    source_origin TEXT NOT NULL, document_id TEXT NOT NULL,
    event_cluster_id TEXT NOT NULL, confirmation_status TEXT NOT NULL,
    entity_impact TEXT NOT NULL, official INTEGER NOT NULL DEFAULT 0, title TEXT,
    original_values TEXT NOT NULL, normalized_values TEXT NOT NULL,
    payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, retrieved_at TEXT NOT NULL,
    UNIQUE(event_id,content_hash)
);
CREATE INDEX IF NOT EXISTS idx_research_event_revisions_visible
    ON research_event_revisions(event_id,retrieved_at);
INSERT INTO research_event_revisions
    (revision_id,event_id,content_hash,supersedes_revision_id,symbol,event_type,event_date,
     effective_at,direction,confidence,materiality,surprise,novelty,source_family,
     source_origin,document_id,event_cluster_id,confirmation_status,entity_impact,official,
     title,original_values,normalized_values,payload,first_seen_at,retrieved_at)
SELECT md5(event_id || ':' || retrieved_at || ':' || payload),event_id,
       md5(symbol || ':' || event_type || ':' || direction::TEXT || ':' || confidence::TEXT
           || ':' || materiality::TEXT || ':' || surprise::TEXT || ':' || novelty::TEXT
           || ':' || payload),NULL,symbol,event_type,event_date,effective_at,direction,
       confidence,materiality,surprise,novelty,source_family,source_origin,document_id,
       event_cluster_id,confirmation_status,entity_impact,official,title,original_values,
       normalized_values,payload,retrieved_at,retrieved_at
FROM research_event_records
ON CONFLICT(event_id,content_hash) DO NOTHING;
ALTER TABLE research_daily_bars ADD COLUMN IF NOT EXISTS dataset_id TEXT;
ALTER TABLE research_valuations ADD COLUMN IF NOT EXISTS requested_as_of TEXT;
ALTER TABLE research_valuations ADD COLUMN IF NOT EXISTS provider_effective_as_of TEXT;
ALTER TABLE research_valuations ADD COLUMN IF NOT EXISTS dataset_id TEXT;
ALTER TABLE research_valuations ADD COLUMN IF NOT EXISTS dividend_yield DOUBLE PRECISION;
ALTER TABLE research_financial_facts ADD COLUMN IF NOT EXISTS first_seen_at TEXT;
UPDATE research_financial_facts SET first_seen_at=retrieved_at
WHERE first_seen_at IS NULL OR first_seen_at='';
CREATE TABLE IF NOT EXISTS selection_input_manifests (
    manifest_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
    decision_context TEXT NOT NULL, universe_snapshot_id TEXT NOT NULL,
    market_dataset_ids TEXT NOT NULL, valuation_dataset_ids TEXT NOT NULL,
    financial_revision_set_id TEXT NOT NULL, event_dataset_id TEXT NOT NULL,
    policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
    policy_payload TEXT NOT NULL, code_revision TEXT NOT NULL,
    dependency_lock_hash TEXT, strategy_snapshot TEXT,
    publication_generations TEXT,
    schema_version TEXT NOT NULL, created_at TEXT NOT NULL
);
ALTER TABLE selection_input_manifests
    ADD COLUMN IF NOT EXISTS dependency_lock_hash TEXT;
ALTER TABLE selection_input_manifests
    ADD COLUMN IF NOT EXISTS strategy_snapshot TEXT;
ALTER TABLE selection_input_manifests
    ADD COLUMN IF NOT EXISTS publication_generations TEXT;
CREATE TABLE IF NOT EXISTS selection_artifacts (
    artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
    parent_snapshot_id TEXT, rule_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
    payload_hash TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(run_id, artifact_type)
);
CREATE TABLE IF NOT EXISTS research_artifacts (
    artifact_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    run_id TEXT NOT NULL,
    formal INTEGER NOT NULL,
    schema_version TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id,artifact_kind)
);
CREATE INDEX IF NOT EXISTS idx_research_artifacts_subject
    ON research_artifacts(subject,created_at DESC);
CREATE TABLE IF NOT EXISTS research_artifact_annotations (
    annotation_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    annotation_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selection_strategy_runs (
    strategy_run_id TEXT PRIMARY KEY, selection_run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    lane TEXT NOT NULL, status TEXT NOT NULL, data_as_of TEXT,
    input_snapshot_id TEXT, policy_version TEXT NOT NULL,
    metadata TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(selection_run_id,strategy_id,strategy_version)
);
CREATE TABLE IF NOT EXISTS selection_candidate_nominations (
    nomination_id TEXT PRIMARY KEY, selection_run_id TEXT NOT NULL,
    strategy_run_id TEXT NOT NULL, symbol TEXT NOT NULL, lane TEXT NOT NULL,
    strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
    lane_rank INTEGER NOT NULL, lane_score_raw DOUBLE PRECISION,
    priority_weight DOUBLE PRECISION NOT NULL, eligibility TEXT NOT NULL,
    evidence TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(selection_run_id,strategy_id,strategy_version,symbol)
);
CREATE TABLE IF NOT EXISTS selection_candidate_outcomes (
    nomination_id TEXT NOT NULL, horizon_days INTEGER NOT NULL,
    entry_date TEXT, entry_price DOUBLE PRECISION, exit_date TEXT,
    exit_price DOUBLE PRECISION, return_pct DOUBLE PRECISION,
    benchmark_return_pct DOUBLE PRECISION, max_drawdown_pct DOUBLE PRECISION,
    outcome_status TEXT NOT NULL, evaluated_at TEXT NOT NULL,
    PRIMARY KEY(nomination_id,horizon_days)
);
CREATE TABLE IF NOT EXISTS strategy_policy_versions (
    policy_version TEXT NOT NULL, policy_hash TEXT NOT NULL,
    state TEXT NOT NULL, effective_from TEXT NOT NULL,
    payload TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(policy_version,policy_hash)
);
CREATE TABLE IF NOT EXISTS strategy_adjustment_proposals (
    proposal_id TEXT PRIMARY KEY, base_policy_hash TEXT NOT NULL,
    evidence_snapshot_id TEXT NOT NULL, proposal TEXT NOT NULL,
    validation_status TEXT NOT NULL, validation_reason TEXT,
    applied_policy_hash TEXT, created_at TEXT NOT NULL, applied_at TEXT
);
-- Premarket factual-news slice.  Event identity comes from the upstream source;
-- observations preserve repeated appearances while revisions preserve content changes.
CREATE TABLE IF NOT EXISTS news_events (
    event_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_event_id TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, published_at TEXT NOT NULL,
    first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL, level TEXT, url TEXT,
    source_quality TEXT NOT NULL, raw_payload TEXT NOT NULL,
    UNIQUE(source,source_event_id)
);
CREATE TABLE IF NOT EXISTS news_event_revisions (
    revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, content_hash TEXT NOT NULL,
    title TEXT NOT NULL, content TEXT NOT NULL, raw_payload TEXT NOT NULL,
    first_observed_at TEXT NOT NULL, UNIQUE(event_id,content_hash)
);
CREATE TABLE IF NOT EXISTS news_observations (
    observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
    observed_at TEXT NOT NULL, bucket TEXT NOT NULL, platform_rank INTEGER,
    platform_heat DOUBLE PRECISION, url TEXT, UNIQUE(run_id,event_id)
);
CREATE TABLE IF NOT EXISTS news_event_tags (
    run_id TEXT NOT NULL, event_id TEXT NOT NULL, bucket TEXT NOT NULL,
    keep INTEGER NOT NULL, importance INTEGER NOT NULL,
    information_type TEXT NOT NULL, time_role TEXT NOT NULL,
    market_relevance TEXT NOT NULL, topic_tags TEXT NOT NULL,
    a_share_mapping TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL,
    tagger TEXT NOT NULL, schema_version TEXT NOT NULL,
    evidence_verified INTEGER NOT NULL, tagged_at TEXT NOT NULL,
    PRIMARY KEY(run_id,event_id)
);
CREATE TABLE IF NOT EXISTS news_theme_threads (
    theme_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, first_seen_at TEXT NOT NULL,
    last_catalyst_at TEXT NOT NULL, strength DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL, linked_sector_ids TEXT NOT NULL,
    linked_security_ids TEXT NOT NULL, active_assumptions TEXT NOT NULL,
    invalidation_conditions TEXT NOT NULL, evidence_event_ids TEXT NOT NULL,
    schema_version TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_premarket_runs (
    run_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
    window_start TEXT NOT NULL, background_end TEXT NOT NULL, window_end TEXT NOT NULL,
    input_count INTEGER NOT NULL, deduplicated_count INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL, revised_count INTEGER NOT NULL,
    tagged_count INTEGER NOT NULL, local_fallback_count INTEGER NOT NULL,
    theme_count INTEGER NOT NULL, evidence_coverage DOUBLE PRECISION NOT NULL,
    stage_status TEXT NOT NULL, warnings TEXT NOT NULL, data_notes TEXT NOT NULL,
    brief_payload TEXT NOT NULL, schema_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_events_published
    ON news_events(published_at,source);
CREATE INDEX IF NOT EXISTS idx_news_observations_run
    ON news_observations(run_id,bucket);
CREATE INDEX IF NOT EXISTS idx_news_tags_role
    ON news_event_tags(run_id,time_role,importance);
CREATE INDEX IF NOT EXISTS idx_news_premarket_date
    ON news_premarket_runs(report_date,finished_at);
CREATE INDEX IF NOT EXISTS idx_research_calendar_evidence
    ON research_trade_calendar_evidence(trade_date, is_open);
CREATE INDEX IF NOT EXISTS idx_research_calendar_fetch
    ON research_calendar_fetch_runs(provider, range_end, quality_status);
CREATE INDEX IF NOT EXISTS idx_research_master_published
    ON research_master_snapshot_runs(snapshot_date, published_at);
CREATE INDEX IF NOT EXISTS idx_research_financial_visible
    ON research_financial_facts(table_name, first_seen_as_of, pub_date, symbol);
CREATE INDEX IF NOT EXISTS idx_selection_nominations_symbol
    ON selection_candidate_nominations(symbol,created_at);
CREATE INDEX IF NOT EXISTS idx_selection_strategy_runs
    ON selection_strategy_runs(strategy_id,created_at);
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('4-reproducible-inputs', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('4-financial-availability-time', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('4-manifest-dependency-lock', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
UPDATE selection_runs SET publication_status='published',published_at=created_at
WHERE status='success' AND publication_status='unpublished'
  AND EXISTS (SELECT 1 FROM selection_input_manifests m WHERE m.run_id=selection_runs.run_id)
  AND EXISTS (SELECT 1 FROM selection_artifacts a
              WHERE a.run_id=selection_runs.run_id AND a.artifact_type='formal_top15')
  AND EXISTS (SELECT 1 FROM selection_artifacts a
              WHERE a.run_id=selection_runs.run_id AND a.artifact_type='formal_top5');
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('5-event-revisions-and-publication', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('7-local-fusion', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('8-premarket-facts', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('9-operational-integrity-v5', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;

-- Publish the latest complete legacy master when upgrading an existing install.
-- This is intentionally idempotent and never replaces a newer immutable snapshot.
DO $$
DECLARE
    legacy_date TEXT;
    legacy_count INTEGER;
    legacy_retrieved_at TEXT;
    legacy_snapshot_id TEXT;
    legacy_exchange_counts TEXT;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM research_master_snapshot_runs
        WHERE quality_status='ok' AND published_at IS NOT NULL
    ) THEN
        SELECT snapshot_date, COUNT(*), MAX(retrieved_at)
          INTO legacy_date, legacy_count, legacy_retrieved_at
          FROM research_security_snapshots
         GROUP BY snapshot_date
        HAVING COUNT(*) >= 3000
         ORDER BY snapshot_date DESC
         LIMIT 1;
        IF legacy_date IS NOT NULL THEN
            legacy_snapshot_id := MD5('legacy-master:' || legacy_date || ':' || legacy_count::TEXT);
            SELECT COALESCE(JSONB_OBJECT_AGG(exchange_name, exchange_count), '{}'::JSONB)::TEXT
              INTO legacy_exchange_counts
              FROM (
                  SELECT COALESCE(NULLIF(exchange,''),'unknown') AS exchange_name,
                         COUNT(*) AS exchange_count
                    FROM research_security_snapshots
                   WHERE snapshot_date=legacy_date
                   GROUP BY COALESCE(NULLIF(exchange,''),'unknown')
              ) AS exchange_summary;
            INSERT INTO research_master_snapshot_runs
                (snapshot_id,snapshot_date,provider,retrieved_at,observed_count,
                 expected_min_count,exchange_counts,previous_snapshot_id,quality_status,
                 published_at,detail)
            VALUES
                (legacy_snapshot_id,legacy_date,'legacy-migration',
                 COALESCE(legacy_retrieved_at,NOW()::TEXT),legacy_count,3000,
                 legacy_exchange_counts,NULL,'ok',COALESCE(legacy_retrieved_at,NOW()::TEXT),
                 '{"source":"research_security_snapshots","migration":"v6"}')
            ON CONFLICT(snapshot_id) DO NOTHING;
            INSERT INTO research_security_master_rows
                (snapshot_id,symbol,ts_code,name,exchange,market,industry,list_status,
                 list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                 schema_version,quality_status,payload)
            SELECT legacy_snapshot_id,symbol,ts_code,name,exchange,market,industry,list_status,
                   list_date,delist_date,is_hs,provider,origin,effective_at,retrieved_at,
                   schema_version,quality_status,payload
              FROM research_security_snapshots WHERE snapshot_date=legacy_date
            ON CONFLICT(snapshot_id,symbol) DO NOTHING;
        END IF;
    END IF;
END $$;
INSERT INTO research_schema_migrations(version, applied_at)
VALUES ('6-publish-legacy-master', NOW()::TEXT)
ON CONFLICT(version) DO NOTHING;

-- Runtime-neutral Agent preview Runs.  These rows never replace formal research/selection facts.
CREATE TABLE IF NOT EXISTS foliant_runs (
    run_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    mode TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_payload TEXT NOT NULL,
    request_id TEXT,
    status TEXT NOT NULL,
    cancellable INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    resource_uri TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    result_payload TEXT,
    provenance TEXT NOT NULL DEFAULT '{}',
    warnings TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    worker_id TEXT,
    fencing_token TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    next_attempt_at TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    updated_at TEXT NOT NULL,
    UNIQUE(actor_id, capability, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_foliant_runs_status ON foliant_runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_foliant_runs_actor ON foliant_runs(actor_id, created_at);
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS fencing_token TEXT;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS lease_until TEXT;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS heartbeat_at TEXT;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 2;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS next_attempt_at TEXT;
ALTER TABLE foliant_runs ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER NOT NULL DEFAULT 1800;
CREATE INDEX IF NOT EXISTS idx_foliant_runs_claim
    ON foliant_runs(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS foliant_run_attempts (
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    fencing_token TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    PRIMARY KEY(run_id, attempt),
    UNIQUE(fencing_token)
);
CREATE TABLE IF NOT EXISTS foliant_run_progress (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    phase TEXT NOT NULL,
    current_value INTEGER,
    total_value INTEGER,
    message_code TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_foliant_run_progress_run
    ON foliant_run_progress(run_id, created_at);
CREATE TABLE IF NOT EXISTS foliant_domain_outbox (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    resource_uri TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT,
    lease_until TEXT,
    last_error TEXT,
    dead_letter_at TEXT,
    UNIQUE(run_id, event_type)
);
ALTER TABLE foliant_domain_outbox ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE foliant_domain_outbox ADD COLUMN IF NOT EXISTS lease_until TEXT;
ALTER TABLE foliant_domain_outbox ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE foliant_domain_outbox ADD COLUMN IF NOT EXISTS dead_letter_at TEXT;
CREATE INDEX IF NOT EXISTS idx_foliant_outbox_pending
    ON foliant_domain_outbox(published_at, created_at);
CREATE TABLE IF NOT EXISTS foliant_write_idempotency (
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(actor_id, action, idempotency_key)
);


-- =============================================================================
-- 验证查询：列出所有已创建的表
-- =============================================================================
-- SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;
