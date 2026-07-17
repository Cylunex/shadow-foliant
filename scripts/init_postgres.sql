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
    last_price            DOUBLE PRECISION,
    last_check_at         TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
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


-- =============================================================================
-- 验证查询：列出所有已创建的表
-- =============================================================================
-- SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name;
