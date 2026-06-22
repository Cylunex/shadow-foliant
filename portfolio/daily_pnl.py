"""每日收益快照 — 盘后统一计算股票+基金单日收益并存表。
供早间 08:50 morning_pnl 只读库发通知，零 API 调用。
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

if not any(os.path.basename(p) == "shadow-foliant" for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402

from db_compat import connect, is_postgres  # noqa: E402


_DB_FILE = "stock_portfolio_snapshots.db"


def _conn():
    return connect(_bootstrap.db_path(_DB_FILE), check_same_thread=False) if not is_postgres() else connect()


def init_db():
    num = "DOUBLE PRECISION" if is_postgres() else "REAL"
    ts = "TIMESTAMPTZ DEFAULT NOW()" if is_postgres() else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS daily_pnl_snapshots (
        snap_date DATE PRIMARY KEY,
        stock_count INTEGER DEFAULT 0,
        stock_mv {num} DEFAULT 0,
        stock_daily_pnl {num} DEFAULT 0,
        stock_daily_pct {num} DEFAULT 0,
        fund_count INTEGER DEFAULT 0,
        fund_mv {num} DEFAULT 0,
        fund_daily_pnl {num} DEFAULT 0,
        fund_daily_pct {num} DEFAULT 0,
        total_daily_pnl {num} DEFAULT 0,
        total_daily_pct {num} DEFAULT 0,
        created_at {ts})""")
    conn.commit()
    conn.close()


def merge_save(snap_date: str) -> Optional[Dict]:
    """22:30 调用：读股票快照(含 daily_mv_change) + 已有基金数据 → 合并写入 daily_pnl_snapshots。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()

    # ─── 股票：从本轮 snapshot 取 daily_mv_change ───
    stock_count = stock_mv = stock_daily_pnl = stock_daily_pct = 0

    def _read_today_snapshot():
        cur.execute(
            """SELECT snap_date, total_mv, n_stocks, daily_mv_change, total_cost
               FROM stock_portfolio_snapshots WHERE snap_date=? ORDER BY snap_date DESC LIMIT 1""",
            (snap_date,),
        )
        return cur.fetchone()

    try:
        row = _read_today_snapshot()
        # ⭐ 2026-06-18 兜底:portfolio_indicator_snapshot(15:45) 卡死被杀时, stock_portfolio_snapshots
        # 不会写当日 row → 这里读到 None → 推送显示"股票 +0(0只)"虚假数据。
        # 缺当日快照时立即调 portfolio_snapshot.save_snapshot 现场算一份, 再读。
        if row is None or row[3] is None:
            try:
                from portfolio.portfolio_snapshot import save_snapshot as _save_snap
                print(f'[daily_pnl] 当日 stock_portfolio_snapshots 缺失, 兜底现场算({snap_date})')
                _save_snap(snap_date)
                row = _read_today_snapshot()
            except Exception as e:
                print(f'[daily_pnl] 兜底 save_snapshot 失败: {type(e).__name__}: {str(e)[:80]}')
        if row and row[3] is not None:
            stock_mv = float(row[1])
            stock_count = int(row[2] or 0)
            stock_daily_pnl = float(row[3])
            prev_mv = stock_mv - stock_daily_pnl
            stock_daily_pct = (stock_daily_pnl / prev_mv * 100) if prev_mv > 0 else 0
    except Exception:
        pass

    # ─── 基金：读已有记录（fund_nav_refresh 22:00 写入） ───
    fund_count = fund_mv = fund_daily_pnl = fund_daily_pct = 0
    cur.execute(
        """SELECT fund_count, fund_mv, fund_daily_pnl, fund_daily_pct
           FROM daily_pnl_snapshots WHERE snap_date=?""",
        (snap_date,),
    )
    existing = cur.fetchone()
    fund_pending = False
    if existing is None:
        # fund_nav_refresh(22:00)没写(失败/迟到/净值源抖动)→ 现场补算一次基金当日(与股票侧兜底对称),
        # 避免把"基金数据缺失"静默写成"基金 0 收益"并推送虚假数字。
        try:
            from fund_db import save_portfolio_snapshot as _fund_snap
            _fund_snap(snap_date)
            cur.execute(
                """SELECT fund_count, fund_mv, fund_daily_pnl, fund_daily_pct
                   FROM daily_pnl_snapshots WHERE snap_date=?""", (snap_date,))
            existing = cur.fetchone()
        except Exception as _fe:
            print(f'[daily_pnl] 基金当日兜底现场算失败: {type(_fe).__name__}: {str(_fe)[:80]}')
            fund_pending = True   # 取数异常 → 基金数据确实缺失,标注供推送提示,而非按 0
    if existing:
        fund_count = int(existing[0] or 0)
        fund_mv = float(existing[1] or 0)
        fund_daily_pnl = float(existing[2] or 0)
        fund_daily_pct = float(existing[3] or 0)

    total_daily_pnl = stock_daily_pnl + fund_daily_pnl
    total_prev = (stock_mv - stock_daily_pnl) + (fund_mv - fund_daily_pnl)
    total_daily_pct = (total_daily_pnl / total_prev * 100) if total_prev > 0 else 0

    cur.execute("DELETE FROM daily_pnl_snapshots WHERE snap_date=?", (snap_date,))
    cur.execute(
        """INSERT INTO daily_pnl_snapshots
           (snap_date, stock_count, stock_mv, stock_daily_pnl, stock_daily_pct,
            fund_count, fund_mv, fund_daily_pnl, fund_daily_pct,
            total_daily_pnl, total_daily_pct)
           VALUES (?,?,?,?,?, ?,?,?,?, ?,?)""",
        (
            snap_date,
            stock_count,
            round(stock_mv, 2),
            round(stock_daily_pnl, 2),
            round(stock_daily_pct, 4),
            fund_count,
            round(fund_mv, 2),
            round(fund_daily_pnl, 2),
            round(fund_daily_pct, 4),
            round(total_daily_pnl, 2),
            round(total_daily_pct, 4),
        ),
    )
    conn.commit()
    conn.close()
    return {
        "snap_date": snap_date,
        "stock_count": stock_count,
        "stock_mv": round(stock_mv, 2),
        "stock_daily_pnl": round(stock_daily_pnl, 2),
        "stock_daily_pct": round(stock_daily_pct, 4),
        "fund_count": fund_count,
        "fund_mv": round(fund_mv, 2),
        "fund_daily_pnl": round(fund_daily_pnl, 2),
        "fund_daily_pct": round(fund_daily_pct, 4),
        "total_daily_pnl": round(total_daily_pnl, 2),
        "total_daily_pct": round(total_daily_pct, 4),
        "fund_pending": fund_pending,   # True=基金数据缺失,推送/展示应标注而非当真 0
    }


def upsert_fund_pnl(snap_date: str, fund_count: int, fund_mv: float,
                     fund_daily_pnl: float, fund_daily_pct: float):
    """由 fund_nav_refresh 调用：写入基金收益部分（仅更新当日基金字段，不碰其他）。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT snap_date FROM daily_pnl_snapshots WHERE snap_date=?", (snap_date,))
    exists = cur.fetchone()
    if exists:
        cur.execute(
            """UPDATE daily_pnl_snapshots SET
               fund_count=?, fund_mv=?, fund_daily_pnl=?, fund_daily_pct=?,
               total_daily_pnl = COALESCE(stock_daily_pnl,0) + ?,
               total_daily_pct = CASE WHEN (COALESCE(stock_mv,0) - COALESCE(stock_daily_pnl,0) + (? - ?)) > 0
                   THEN (COALESCE(stock_daily_pnl,0) + ?) / (COALESCE(stock_mv,0) - COALESCE(stock_daily_pnl,0) + (? - ?)) * 100
                   ELSE 0 END
               WHERE snap_date=?""",
            (fund_count, fund_mv, fund_daily_pnl, fund_daily_pct,
             fund_daily_pnl, fund_mv, fund_daily_pnl, fund_daily_pnl, fund_mv, fund_daily_pnl, snap_date),
        )
    else:
        cur.execute(
            """INSERT INTO daily_pnl_snapshots
               (snap_date, fund_count, fund_mv, fund_daily_pnl, fund_daily_pct, total_daily_pnl, total_daily_pct)
               VALUES (?,?,?,?,?,?,?)""",
            (snap_date, fund_count, fund_mv, fund_daily_pnl, fund_daily_pct,
             fund_daily_pnl, fund_daily_pct),
        )
    conn.commit()
    conn.close()


def get_pnl(snap_date: str = None) -> Optional[Dict]:
    """取指定日期的收益快照。snap_date=None 取最新。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    if snap_date:
        cur.execute(
            """SELECT snap_date, stock_count, stock_mv, stock_daily_pnl, stock_daily_pct,
                      fund_count, fund_mv, fund_daily_pnl, fund_daily_pct,
                      total_daily_pnl, total_daily_pct
               FROM daily_pnl_snapshots WHERE snap_date=?""",
            (snap_date,),
        )
    else:
        cur.execute(
            """SELECT snap_date, stock_count, stock_mv, stock_daily_pnl, stock_daily_pct,
                      fund_count, fund_mv, fund_daily_pnl, fund_daily_pct,
                      total_daily_pnl, total_daily_pct
               FROM daily_pnl_snapshots ORDER BY snap_date DESC LIMIT 1"""
        )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    cols = [
        "snap_date", "stock_count", "stock_mv", "stock_daily_pnl", "stock_daily_pct",
        "fund_count", "fund_mv", "fund_daily_pnl", "fund_daily_pct",
        "total_daily_pnl", "total_daily_pct",
    ]
    return dict(zip(cols, row))


def _f(v):
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def get_recent(days: int = 30) -> List[Dict]:
    """近 N 日合并日收益序列(升序)。键:snap_date/stock_daily_pnl/fund_daily_pnl/total_daily_pnl/total_daily_pct/total_mv。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT snap_date, stock_daily_pnl, fund_daily_pnl, total_daily_pnl, total_daily_pct,
                  stock_mv, fund_mv
           FROM daily_pnl_snapshots ORDER BY snap_date DESC LIMIT ?""",
        (int(days),),
    )
    rows = cur.fetchall()
    conn.close()
    out = [{
        "snap_date": str(r[0]), "stock_daily_pnl": _f(r[1]), "fund_daily_pnl": _f(r[2]),
        "total_daily_pnl": _f(r[3]), "total_daily_pct": _f(r[4]),
        "total_mv": round(_f(r[5]) + _f(r[6]), 2),
    } for r in rows]
    out.reverse()
    return out


def get_summary(days: int = 60) -> Dict:
    """日收益汇总:最新一日 + 本月累计 + 区间累计 + 胜率 + 最佳/最差日。无数据返回 {}。"""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT snap_date, total_daily_pnl, total_daily_pct, stock_mv, fund_mv
           FROM daily_pnl_snapshots ORDER BY snap_date DESC LIMIT ?""",
        (max(int(days), 31),),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return {}
    latest = {"snap_date": str(rows[0][0]), "total_daily_pnl": _f(rows[0][1]),
              "total_daily_pct": _f(rows[0][2]), "total_mv": round(_f(rows[0][3]) + _f(rows[0][4]), 2)}
    ym = str(rows[0][0])[:7]
    pnls = [_f(r[1]) for r in rows]
    mtd = sum(_f(r[1]) for r in rows if str(r[0]).startswith(ym))
    win = sum(1 for x in pnls if x > 0)
    return {
        "latest": latest,
        "mtd_pnl": round(mtd, 2),
        "period_pnl": round(sum(pnls), 2),
        "period_days": len(pnls),
        "win_rate": round(win / len(pnls) * 100, 1) if pnls else 0,
        "best_day": round(max(pnls), 2) if pnls else 0,
        "worst_day": round(min(pnls), 2) if pnls else 0,
    }
