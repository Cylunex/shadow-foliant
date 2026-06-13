"""PG 表标准化优化迁移(2026-06-06)—— 数据保留、事务内、可重跑、改前后行数校验。

做的事:
  1. 归一分类列脏数据(rating/trade_type 中英混用 → 中文规范值)。
  2. 系统受控列转 PG 原生 ENUM(job 状态 / 信号 / 通知类型)。
  3. rating/confidence 加中文 CHECK 约束(LLM 喂值,应用层已 normalize_*,这里兜底)。
  4. 价格/金额列 double → NUMERIC(20,4)(指标/比率/百分比保持 double)。
  5. updated_at 自动更新触发器(11 张表)。
  6. 补 4 个缺失的 FK 索引。

用法:  python scripts/migrate_optimize_20260606.py          # 真正执行(改前后行数一致才 COMMIT)
       python scripts/migrate_optimize_20260606.py --dry   # 只演练 + 回滚,不提交
绝不删除任何行;失败自动 ROLLBACK。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa
from dotenv import load_dotenv
load_dotenv()
import psycopg2

DRY = '--dry' in sys.argv

# 价格/金额列(转 NUMERIC);明确列出,避免误伤指标/百分比/比率
MONEY_COLS = {
    'price', 'close', 'high', 'low', 'open', 'cost_price', 'amount', 'target_price',
    'take_profit', 'stop_loss', 'entry_low', 'entry_high', 'ref_price', 'last_price',
    'current_price', 'total_mv', 'total_cost', 'commission', 'tax', 'recommended_price',
    'position_cost', 'pos_cost_price', 'stop_loss_price', 'take_profit_price', 'profit_loss',
}
RATING_TABLES = ['monitored_stocks', 'portfolio_analysis_history', 'ai_recommendations']
RATINGS = ('强烈买入', '买入', '持有', '卖出', '强烈卖出')
UPDATED_AT_TABLES = ['ai_recommendations', 'automation_switches', 'monitor_tasks',
                     'monitored_stocks', 'northbound_flow_daily', 'portfolio_stocks',
                     'position_monitor', 'prompt_templates', 'sector_tracking',
                     'stock_tracking', 'user_strategy_config']


def main():
    conn = psycopg2.connect(host=os.getenv('PG_HOST'), port=int(os.getenv('PG_PORT')),
                            dbname=os.getenv('PG_DATABASE'), user=os.getenv('PG_USER'),
                            password=os.getenv('PG_PASSWORD'))
    conn.autocommit = False
    cur = conn.cursor()
    log = []

    def run(sql, note=''):
        cur.execute(sql)
        log.append(f'  ✓ {note or sql[:70]}  (rows={cur.rowcount})')

    # --- 改前行数快照 ---
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    tables = [r[0] for r in cur.fetchall()]
    before = {}
    for t in tables:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        before[t] = cur.fetchone()[0]

    try:
        # ============ 1. 归一脏数据 ============
        log.append('— 1. 归一分类列 —')
        rmap = [('strong_buy', '强烈买入'), ('buy', '买入'), ('hold', '持有'),
                ('sell', '卖出'), ('strong_sell', '强烈卖出')]
        for t in RATING_TABLES:
            for en, cn in rmap:
                cur.execute(f"UPDATE {t} SET rating=%s WHERE lower(rating)=%s", (cn, en))
                if cur.rowcount:
                    log.append(f'  ✓ {t}.rating {en}→{cn} ×{cur.rowcount}')
        # confidence
        for en, cn in [('high', '高'), ('medium', '中'), ('low', '低')]:
            cur.execute("UPDATE ai_recommendations SET confidence=%s WHERE lower(confidence)=%s", (cn, en))
        # trade_records.trade_type / source
        for en, cn in [('sell', '卖出'), ('delete', '删除'), ('add', '新增'), ('update', '调整')]:
            cur.execute("UPDATE trade_records SET trade_type=%s WHERE lower(trade_type)=%s", (cn, en))
            if cur.rowcount:
                log.append(f'  ✓ trade_records.trade_type {en}→{cn} ×{cur.rowcount}')
        cur.execute("UPDATE trade_records SET source='trade' WHERE source='trade_record'")

        # ============ 2. 系统列 → ENUM ============
        log.append('— 2. 系统受控列转 ENUM —')
        enums = {
            'job_status_enum': ('success', 'error', 'skipped', 'running'),
            'signal_enum': ('BUY', 'SELL', 'HOLD'),
            'notif_type_enum': ('entry', 'take_profit', 'stop_loss'),
        }
        for name, vals in enums.items():
            lits = ','.join(f"'{v}'" for v in vals)
            run(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname='{name}') "
                f"THEN CREATE TYPE {name} AS ENUM ({lits}); END IF; END $$;", f'TYPE {name}')

        def alter_enum(table, col, enum):
            cur.execute("SELECT udt_name FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col))
            r = cur.fetchone()
            if r and r[0] != enum:
                cur.execute(f'ALTER TABLE {table} ALTER COLUMN {col} TYPE {enum} USING {col}::{enum}')
                log.append(f'  ✓ {table}.{col} → {enum}')
        alter_enum('job_runs', 'status', 'job_status_enum')
        # 注:scheduler_logs.status(news_flow 子系统)用 success/failed/error,词汇与 job_status_enum 不同,
        #     保持 TEXT 不转枚举,避免未来写 'failed' 越界失败。
        alter_enum('factor_snapshots', 'signal', 'signal_enum')
        alter_enum('monitor_notifications', 'type', 'notif_type_enum')

        # ============ 3. rating/confidence CHECK ============
        log.append('— 3. rating/confidence CHECK 约束 —')
        rlits = ','.join(f"'{v}'" for v in RATINGS)
        for t in RATING_TABLES:
            run(f"ALTER TABLE {t} DROP CONSTRAINT IF EXISTS chk_{t}_rating", f'drop chk {t}')
            run(f"ALTER TABLE {t} ADD CONSTRAINT chk_{t}_rating CHECK (rating IS NULL OR rating IN ({rlits}))", f'chk {t}.rating')
        run("ALTER TABLE ai_recommendations DROP CONSTRAINT IF EXISTS chk_air_conf")
        run("ALTER TABLE ai_recommendations ADD CONSTRAINT chk_air_conf CHECK (confidence IS NULL OR confidence IN ('高','中','低'))", 'chk confidence')

        # ============ 4. 价格/金额 → NUMERIC ============
        log.append('— 4. 价格/金额列 → NUMERIC(20,4) —')
        cur.execute("SELECT table_name,column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND data_type='double precision' ORDER BY 1,2")
        for t, c in cur.fetchall():
            if c in MONEY_COLS:
                cur.execute(f'ALTER TABLE "{t}" ALTER COLUMN "{c}" TYPE numeric(20,4) USING "{c}"::numeric')
                log.append(f'  ✓ {t}.{c} → numeric(20,4)')

        # ============ 5. updated_at 触发器 ============
        log.append('— 5. updated_at 自动更新触发器 —')
        run("CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS "
            "$$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END; $$ LANGUAGE plpgsql", 'func set_updated_at')
        for t in UPDATED_AT_TABLES:
            run(f"DROP TRIGGER IF EXISTS trg_{t}_updated ON {t}")
            run(f"CREATE TRIGGER trg_{t}_updated BEFORE UPDATE ON {t} "
                f"FOR EACH ROW EXECUTE FUNCTION set_updated_at()", f'trigger {t}')

        # ============ 6. 补 FK 索引 ============
        log.append('— 6. 补缺失 FK 索引 —')
        for t, c in [('sector_tracking', 'analysis_id'), ('ai_analysis', 'snapshot_id'),
                     ('sentiment_records', 'snapshot_id'), ('trade_records', 'change_id')]:
            run(f"CREATE INDEX IF NOT EXISTS idx_{t}_{c} ON {t} ({c})", f'idx {t}.{c}')

        # ============ 校验:改前后行数一致 ============
        after = {}
        for t in tables:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            after[t] = cur.fetchone()[0]
        diffs = {t: (before[t], after[t]) for t in tables if before[t] != after[t]}

        print('\n'.join(log))
        if diffs:
            print('\n❌ 行数变化(异常,回滚):', diffs)
            conn.rollback()
            return
        print(f'\n✅ 全部表行数一致(共 {sum(before.values())} 行,未增删)。')
        if DRY:
            conn.rollback()
            print('🔙 --dry 演练完成,已回滚(未提交)。')
        else:
            conn.commit()
            print('💾 已 COMMIT。')
    except Exception as e:
        conn.rollback()
        print('\n'.join(log))
        print(f'\n❌ 迁移失败,已 ROLLBACK: {type(e).__name__}: {e}')
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
