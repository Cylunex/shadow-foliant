"""
PostgreSQL 数据库适配模块 — 取代原有的 SQLite database.py + longhubang_db.py

依赖: pip install psycopg2-binary
使用方式:
    from database_pg import db
    db.save_analysis(...)
    db.get_all_records(...)
"""

import json
import os
from datetime import datetime

# 确保 .env 环境变量已加载
try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

import psycopg2
import psycopg2.extras
import pandas as pd

# ==================== 配置 ====================

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "127.0.0.1"),
    "port": int(os.getenv("PG_PORT", "55432")),
    "dbname": os.getenv("PG_DATABASE", "aiagents_stock"),
    "user": os.getenv("PG_USER", "aiagents_stock"),
    "password": os.getenv("PG_PASSWORD", "changeme"),
}


def get_conn():
    """获取数据库连接"""
    return psycopg2.connect(**DB_CONFIG)


# ==================== 分析记录模块 (原 database.py) ====================

class StockAnalysisDatabasePG:
    """AI 股票分析记录 — PostgreSQL 版"""

    def init_database(self):
        """表已通过 CREATE TABLE IF NOT EXISTS 创建，此方法保留兼容"""
        pass

    def save_analysis(self, symbol, stock_name, period,
                      stock_info, agents_results, discussion_result, final_decision):
        """保存 AI 分析 — PG 模式只存关键结果，丢弃大段 prompt 文本

        精简策略（节省 PG 空间，提速查询）：
          - stock_info: 仅保留 9 个关键字段
          - agents_results: 每个 agent 只存名字+1-2 句结论（截断 400 字符）
          - discussion_result: 仅存 summary 字段（截断 800 字符）
          - final_decision: 完整保留（rating/target_price/entry/stop/take_profit 等已是精简结构）
        """
        conn = get_conn()
        cur = conn.cursor()
        analysis_date = datetime.now()
        created_at = datetime.now()

        # ---- 1. stock_info 精简：保留关键字段 ----
        slim_info = {}
        if isinstance(stock_info, dict):
            for k in ('symbol', 'name', 'current_price', 'change_percent',
                      'pe_ratio', 'pb_ratio', 'market_cap',
                      'industry', 'market', 'exchange'):
                if k in stock_info:
                    slim_info[k] = stock_info[k]

        # ---- 2. agents_results 精简：每个 agent 名+结论摘要 ----
        slim_agents = {}
        if isinstance(agents_results, dict):
            for agent_key, agent_data in agents_results.items():
                if isinstance(agent_data, dict):
                    analysis_text = str(agent_data.get('analysis', ''))[:400]
                    slim_agents[agent_key] = {
                        'agent_name': agent_data.get('agent_name', agent_key),
                        'analysis_summary': analysis_text,
                        'focus_areas': agent_data.get('focus_areas', []),
                        'timestamp': str(agent_data.get('timestamp', '')),
                    }
                else:
                    slim_agents[agent_key] = {'summary': str(agent_data)[:400]}

        # ---- 3. discussion_result 精简：只取摘要 ----
        slim_discussion = {}
        if isinstance(discussion_result, dict):
            slim_discussion = {
                'summary': str(discussion_result.get('summary',
                                                     discussion_result.get('content', '')))[:800],
                'key_points': discussion_result.get('key_points', []),
            }
        elif isinstance(discussion_result, str):
            slim_discussion = {'summary': discussion_result[:800]}

        def to_json(val):
            return json.dumps(val, ensure_ascii=False, default=str)

        cur.execute("""
            INSERT INTO analysis_records
                (symbol, stock_name, analysis_date, period,
                 stock_info, agents_results, discussion_result, final_decision, created_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s)
            RETURNING id
        """, (
            symbol, stock_name, analysis_date, period,
            to_json(slim_info), to_json(slim_agents),
            to_json(slim_discussion), to_json(final_decision),
            created_at
        ))
        record_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return record_id

    def get_all_records(self):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, symbol, stock_name, analysis_date, period,
                   final_decision, created_at
            FROM analysis_records
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for row in rows:
            fd = row["final_decision"]
            if isinstance(fd, dict):
                rating = fd.get("rating", "未知")
            else:
                rating = "未知"
            result.append({
                "id": row["id"],
                "symbol": row["symbol"],
                "stock_name": row["stock_name"],
                "analysis_date": str(row["analysis_date"]),
                "period": row["period"],
                "rating": rating,
                "created_at": str(row["created_at"]),
            })
        return result

    def get_record_count(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM analysis_records")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count

    def get_record_by_id(self, record_id):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM analysis_records WHERE id = %s", (record_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        result = dict(row)
        # PG timestamp 转字符串，兼容调用方的 .get('created_at', '')[:19] 写法
        from datetime import datetime as _dt
        for _k, _v in result.items():
            if isinstance(_v, _dt):
                result[_k] = str(_v)
        return result

    def delete_record(self, record_id):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM analysis_records WHERE id = %s", (record_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        conn.close()
        return deleted


# ==================== 龙虎榜模块 (原 longhubang_db.py) ====================

class LonghubangDatabasePG:
    """龙虎榜数据 — PostgreSQL 版"""

    def init_database(self):
        pass

    def save_longhubang_data(self, data_list):
        if not data_list:
            return 0
        conn = get_conn()
        cur = conn.cursor()
        saved = 0
        for rec in data_list:
            try:
                cur.execute("""
                    INSERT INTO longhubang_records
                        (date, stock_code, stock_name, youzi_name, yingye_bu,
                         list_type, buy_amount, sell_amount, net_inflow, concepts)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (date, stock_code, youzi_name, yingye_bu)
                    DO UPDATE SET
                        buy_amount = EXCLUDED.buy_amount,
                        sell_amount = EXCLUDED.sell_amount,
                        net_inflow = EXCLUDED.net_inflow,
                        concepts = EXCLUDED.concepts
                """, (
                    rec.get("rq") or rec.get("日期"),
                    rec.get("gpdm") or rec.get("股票代码"),
                    rec.get("gpmc") or rec.get("股票名称"),
                    rec.get("yzmc") or rec.get("游资名称"),
                    rec.get("yyb") or rec.get("营业部"),
                    rec.get("sblx") or rec.get("榜单类型"),
                    float(rec.get("mrje") or rec.get("买入金额") or 0),
                    float(rec.get("mcje") or rec.get("卖出金额") or 0),
                    float(rec.get("jlrje") or rec.get("净流入金额") or 0),
                    rec.get("gl") or rec.get("概念"),
                ))
                saved += 1
            except Exception:
                continue
        conn.commit()
        cur.close()
        conn.close()
        return saved

    def get_longhubang_data(self, start_date=None, end_date=None, stock_code=None):
        conn = get_conn()
        query = "SELECT * FROM longhubang_records WHERE 1=1"
        params = []
        if start_date:
            query += " AND date >= %s"
            params.append(start_date)
        if end_date:
            query += " AND date <= %s"
            params.append(end_date)
        if stock_code:
            query += " AND stock_code = %s"
            params.append(stock_code)
        query += " ORDER BY date DESC, net_inflow DESC"
        df = pd.read_sql(query, conn, params=params)
        conn.close()
        return df

    def get_top_youzi(self, start_date=None, end_date=None, limit=20):
        conn = get_conn()
        query = """
            SELECT youzi_name,
                   COUNT(*) AS trade_count,
                   SUM(buy_amount) AS total_buy,
                   SUM(sell_amount) AS total_sell,
                   SUM(net_inflow) AS total_net_inflow
            FROM longhubang_records
            WHERE 1=1
        """
        params = []
        if start_date:
            query += " AND date >= %s"
            params.append(start_date)
        if end_date:
            query += " AND date <= %s"
            params.append(end_date)
        query += """
            GROUP BY youzi_name
            ORDER BY total_net_inflow DESC
            LIMIT %s
        """
        params.append(limit)
        df = pd.read_sql(query, conn, params=params)
        conn.close()
        return df

    def get_top_stocks(self, start_date=None, end_date=None, limit=20):
        conn = get_conn()
        query = """
            SELECT stock_code, stock_name,
                   COUNT(DISTINCT youzi_name) AS youzi_count,
                   SUM(buy_amount) AS total_buy,
                   SUM(sell_amount) AS total_sell,
                   SUM(net_inflow) AS total_net_inflow,
                   STRING_AGG(DISTINCT concepts, ', ') AS all_concepts
            FROM longhubang_records
            WHERE 1=1
        """
        params = []
        if start_date:
            query += " AND date >= %s"
            params.append(start_date)
        if end_date:
            query += " AND date <= %s"
            params.append(end_date)
        query += """
            GROUP BY stock_code, stock_name
            ORDER BY total_net_inflow DESC
            LIMIT %s
        """
        params.append(limit)
        df = pd.read_sql(query, conn, params=params)
        conn.close()
        return df

    def save_analysis_report(self, data_date_range, analysis_content,
                             recommended_stocks, summary, full_result=None):
        conn = get_conn()
        cur = conn.cursor()
        if isinstance(analysis_content, dict):
            analysis_content = json.dumps(analysis_content, ensure_ascii=False, indent=2)
        cur.execute("""
            INSERT INTO longhubang_analysis
                (analysis_date, data_date_range, analysis_content,
                 recommended_stocks, summary)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            RETURNING id
        """, (
            datetime.now(),
            data_date_range,
            analysis_content,
            json.dumps(recommended_stocks, ensure_ascii=False),
            summary
        ))
        report_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return report_id

    def get_analysis_reports(self, limit=10):
        conn = get_conn()
        df = pd.read_sql("""
            SELECT * FROM longhubang_analysis
            ORDER BY created_at DESC
            LIMIT %s
        """, conn, params=[limit])
        conn.close()
        return df

    def get_analysis_report(self, report_id):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM longhubang_analysis WHERE id = %s", (report_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return dict(row)
        return None

    def delete_analysis_report(self, report_id):
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM stock_tracking WHERE analysis_id = %s", (report_id,))
            cur.execute("DELETE FROM longhubang_analysis WHERE id = %s", (report_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            return False
        finally:
            cur.close()
            conn.close()

    def get_statistics(self):
        conn = get_conn()
        cur = conn.cursor()
        stats = {}
        cur.execute("SELECT COUNT(*) FROM longhubang_records")
        stats["total_records"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT stock_code) FROM longhubang_records")
        stats["total_stocks"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT youzi_name) FROM longhubang_records")
        stats["total_youzi"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM longhubang_analysis")
        stats["total_reports"] = cur.fetchone()[0]
        cur.execute("SELECT MIN(date), MAX(date) FROM longhubang_records")
        dr = cur.fetchone()
        stats["date_range"] = {"start": str(dr[0]), "end": str(dr[1])}
        cur.close()
        conn.close()
        return stats


# ==================== 全局实例 ====================

db = StockAnalysisDatabasePG()
longhubang_db = LonghubangDatabasePG()


if __name__ == "__main__":
    sep = "=" * 50
    print(sep)
    print("  PostgreSQL 数据库模块 — 自检")
    print(sep)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """)
    for t in cur.fetchall():
        cur.execute("SELECT COUNT(*) FROM %s" % t[0])
        cnt = cur.fetchone()[0]
        print("  ├─ %-25s %s 条记录" % (t[0], cnt))
    cur.close()
    conn.close()
    print()
    print("  ✅ 数据库模块已就绪，可在代码中直接引用：")
    print('     from database_pg import db, longhubang_db')
    print(sep)
