"""
持仓股票数据库管理模块 — PostgreSQL 版
替代原有的 portfolio_db.py (SQLite)

使用:
    from portfolio_db_pg import portfolio_db
    portfolio_db.get_all_stocks()
    portfolio_db.add_stock("000001", "平安银行", 12.5, 1000)
"""

import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "127.0.0.1"),
    "port": int(os.getenv("PG_PORT", "55432")),
    "dbname": os.getenv("PG_DATABASE", "aiagents_stock"),
    "user": os.getenv("PG_USER", "aiagents_stock"),
    "password": os.getenv("PG_PASSWORD", "changeme"),
}


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


class PortfolioDBPG:
    """持仓股票数据库管理类 — PostgreSQL 版"""

    # ==================== 持仓股票 CRUD ====================

    # ============================================================
    # 持仓变动自动记录 — 写入 trade_records 表（合并 portfolio_changes）
    # ============================================================
    def _log_change(self, code, name, change_type, old_data, new_data,
                     cost_price=None, quantity=None, source='ui_manual',
                     note=None, conn=None, cur=None, trade_time=None):
        """记录持仓变动到 trade_records；如果调用方传了 conn/cur 复用避免新连接。
        trade_time: 实际成交时间(导入成交记录时传入),缺省用 NOW()。"""
        own_conn = conn is None
        if own_conn:
            conn = get_conn()
            cur = conn.cursor()
        try:
            delta_qty = None
            if old_data and new_data:
                oq = (old_data.get('quantity') or 0)
                nq = (new_data.get('quantity') or 0)
                delta_qty = nq - oq if (oq or nq) else None
            elif new_data and not old_data:
                delta_qty = new_data.get('quantity')
            elif old_data and not new_data:
                delta_qty = -(old_data.get('quantity') or 0)

            # 从 new_data 中获取变更后持仓状态
            pos_qty = (new_data or old_data or {}).get('quantity')
            pos_cost = (new_data or old_data or {}).get('cost_price')
            if not new_data:
                # delete 场景：pos_qty=0，pos_cost null
                pos_qty = 0

            cur.execute("""
                INSERT INTO trade_records
                    (stock_code, stock_name, trade_type, price, quantity, amount,
                     pos_quantity, pos_cost_price, delta_qty, source, note, trade_time)
                VALUES (%s, %s, %s, NULL, NULL, NULL,
                        %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, NOW()))
            """, (code, name, change_type,
                   pos_qty, pos_cost, delta_qty, source, note,
                   (str(trade_time) if trade_time else None)))
            if own_conn:
                conn.commit()
        except Exception as e:
            print(f'[trade_records] 记录失败（不影响主操作）: {e}')
            if own_conn:
                conn.rollback()
        finally:
            if own_conn:
                cur.close()
                conn.close()

    def add_stock(self, code: str, name: str,
                  cost_price: Optional[float] = None,
                  quantity: Optional[int] = None,
                  note: str = "",
                  auto_monitor: bool = True,
                  source: str = 'ui_manual',
                  trade_time=None,
                  log_change: bool = True) -> int:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO portfolio_stocks
                    (code, name, cost_price, quantity, note, auto_monitor,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (code, name, cost_price, quantity, note, auto_monitor,
                  datetime.now(), datetime.now()))
            stock_id = cur.fetchone()[0]
            conn.commit()
            # 记录变动(导入成交时 log_change=False,避免往 trade_records 写空的 update 日志)
            if log_change:
                new_data = {'code': code, 'name': name, 'cost_price': cost_price,
                            'quantity': quantity, 'note': note}
                self._log_change(code, name, 'add', None, new_data,
                                 cost_price=cost_price, quantity=quantity, source=source,
                                 trade_time=trade_time)
            return stock_id
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            raise ValueError(f"股票代码 {code} 已存在")
        finally:
            cur.close()
            conn.close()

    def update_stock(self, stock_id: int, source: str = 'ui_manual', trade_time=None,
                     log_change: bool = True, **kwargs) -> bool:
        allowed = {'code', 'name', 'cost_price', 'quantity', 'note', 'auto_monitor'}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        # 获取旧数据
        old = self.get_stock(stock_id)
        if not old:
            return False
        fields['updated_at'] = datetime.now()
        set_clause = ", ".join(f"{k} = %s" for k in fields)
        values = list(fields.values()) + [stock_id]

        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(f"UPDATE portfolio_stocks SET {set_clause} WHERE id = %s", values)
            conn.commit()
            if cur.rowcount > 0:
                if log_change:
                    new_data = {**old, **fields}
                    self._log_change(old['code'], old.get('name'), 'update',
                                     {k: old.get(k) for k in ['code', 'name', 'cost_price', 'quantity', 'note']},
                                     {k: new_data.get(k) for k in ['code', 'name', 'cost_price', 'quantity', 'note']},
                                     cost_price=new_data.get('cost_price'),
                                     quantity=new_data.get('quantity'),
                                     source=source, trade_time=trade_time)
                return True
            return False
        finally:
            cur.close()
            conn.close()

    def delete_stock(self, stock_id: int, source: str = 'ui_manual') -> bool:
        old = self.get_stock(stock_id)
        if not old:
            return False
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM portfolio_stocks WHERE id = %s", (stock_id,))
            conn.commit()
            if cur.rowcount > 0:
                old_data = {k: old.get(k) for k in ['code', 'name', 'cost_price', 'quantity', 'note']}
                self._log_change(old['code'], old.get('name'), 'delete',
                                 old_data, None,
                                 cost_price=old.get('cost_price'),
                                 quantity=old.get('quantity'),
                                 source=source)
                return True
            return False
        finally:
            cur.close()
            conn.close()

    def get_stock(self, stock_id: int) -> Optional[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("SELECT * FROM portfolio_stocks WHERE id = %s", (stock_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            cur.close()
            conn.close()

    def get_stock_by_code(self, code: str) -> Optional[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("SELECT * FROM portfolio_stocks WHERE code = %s", (code,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            cur.close()
            conn.close()

    def get_all_stocks(self, auto_monitor_only: bool = False,
                       include_cleared: bool = False) -> List[Dict]:
        """include_cleared=False 默认过滤 quantity=0 的清仓行(NULL 保留, 视为"未填数量")。
        2026-06-17: 此前清仓后行仍在表里, 持仓分析/监控/AI 还在跑已清仓股票。"""
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            where = []
            if auto_monitor_only:
                where.append('auto_monitor = TRUE')
            if not include_cleared:
                where.append('(quantity IS NULL OR quantity != 0)')
            sql = 'SELECT * FROM portfolio_stocks'
            if where:
                sql += ' WHERE ' + ' AND '.join(where)
            sql += ' ORDER BY created_at DESC'
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def search_stocks(self, keyword: str) -> List[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""
                SELECT * FROM portfolio_stocks
                WHERE code ILIKE %s OR name ILIKE %s
                ORDER BY created_at DESC
            """, (f"%{keyword}%", f"%{keyword}%"))
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def get_stock_count(self) -> int:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM portfolio_stocks")
            return cur.fetchone()[0]
        finally:
            cur.close()
            conn.close()

    # ==================== 批量导入 ====================

    def bulk_import(self, stocks: List[Dict], mode: str = 'upsert',
                    source: str = 'bulk_import') -> Dict[str, int]:
        """批量导入持仓

        Args:
            stocks: 股票列表，每条 dict 至少含 code，可选 name/cost_price/quantity/note
            mode:
                'upsert'  — 已存在则更新，不存在则新增（默认）
                'replace' — 先清空全部持仓再插入新清单
                'add'     — 只新增（已存在则跳过）
            source: 来源标记（写入 portfolio_changes.source）

        Returns:
            {'added': N, 'updated': N, 'skipped': N, 'deleted': N (mode=replace 时)}
        """
        added = updated = skipped = deleted = 0
        conn = get_conn()
        cur = conn.cursor()
        try:
            if mode == 'replace':
                # 记录所有现有持仓为 delete
                cur.execute('SELECT id, code, name, cost_price, quantity, note FROM portfolio_stocks')
                for r in cur.fetchall():
                    self._log_change(r[1], r[2], 'delete',
                                     {'code': r[1], 'name': r[2], 'cost_price': float(r[3]) if r[3] else None,
                                      'quantity': r[4], 'note': r[5]},
                                     None, cost_price=r[3], quantity=r[4],
                                     source=source, conn=conn, cur=cur)
                    deleted += 1
                cur.execute('DELETE FROM portfolio_stocks')

            for s in stocks:
                code = s.get('code') or s.get('股票代码') or s.get('symbol')
                if not code:
                    skipped += 1
                    continue
                code = str(code).strip().zfill(6) if str(code).strip().isdigit() and len(str(code).strip()) <= 6 else str(code).strip()
                name = s.get('name') or s.get('股票名称') or s.get('股票简称') or code
                cost_price = s.get('cost_price') or s.get('成本价') or s.get('cost')
                quantity = s.get('quantity') or s.get('数量') or s.get('qty')
                note = s.get('note') or s.get('备注') or ''
                try:
                    cost_price = float(cost_price) if cost_price not in (None, '') else None
                except (TypeError, ValueError):
                    cost_price = None
                try:
                    quantity = int(quantity) if quantity not in (None, '') else None
                except (TypeError, ValueError):
                    quantity = None

                # 检查是否存在
                cur.execute('SELECT id, name, cost_price, quantity, note FROM portfolio_stocks WHERE code = %s', (code,))
                existing = cur.fetchone()

                if existing:
                    if mode == 'add':
                        skipped += 1
                        continue
                    # update
                    old_data = {'code': code, 'name': existing[1],
                                'cost_price': float(existing[2]) if existing[2] else None,
                                'quantity': existing[3], 'note': existing[4]}
                    cur.execute("""
                        UPDATE portfolio_stocks
                        SET name=%s, cost_price=%s, quantity=%s, note=%s, updated_at=%s
                        WHERE code=%s
                    """, (name, cost_price, quantity, note, datetime.now(), code))
                    new_data = {'code': code, 'name': name, 'cost_price': cost_price,
                                'quantity': quantity, 'note': note}
                    self._log_change(code, name, 'update', old_data, new_data,
                                     cost_price=cost_price, quantity=quantity,
                                     source=source, conn=conn, cur=cur)
                    updated += 1
                else:
                    cur.execute("""
                        INSERT INTO portfolio_stocks
                            (code, name, cost_price, quantity, note, auto_monitor,
                             created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
                    """, (code, name, cost_price, quantity, note,
                          datetime.now(), datetime.now()))
                    new_data = {'code': code, 'name': name, 'cost_price': cost_price,
                                'quantity': quantity, 'note': note}
                    self._log_change(code, name, 'add', None, new_data,
                                     cost_price=cost_price, quantity=quantity,
                                     source=source, conn=conn, cur=cur)
                    added += 1
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
        return {'added': added, 'updated': updated, 'skipped': skipped, 'deleted': deleted}

    # ==================== 批量导入成交记录 ====================

    def _normalize_trade(self, t: Dict):
        """把一条成交 dict 标准化;非法返回 None。"""
        SELL = {'卖出', '卖', 'sell', 's', 'S', '减仓'}
        code = t.get('code') or t.get('股票代码') or t.get('symbol')
        qty = t.get('quantity') or t.get('数量') or t.get('qty')
        price = t.get('price') or t.get('价格') or t.get('成交价')
        if not code or qty in (None, '') or price in (None, ''):
            return None
        code = str(code).strip()
        code = code.zfill(6) if code.isdigit() and len(code) <= 6 else code.upper()
        name = t.get('name') or t.get('股票名称') or t.get('股票简称')
        raw = str(t.get('trade_type') or t.get('方向') or t.get('direction') or '买入').strip()
        ttype = '卖出' if raw in SELL else '买入'
        qty = int(float(qty)); price = float(price)
        amount = t.get('amount') or t.get('金额')
        amount = float(amount) if amount not in (None, '') else round(qty * price, 2)
        tt = t.get('trade_time') or t.get('成交时间') or t.get('日期') or t.get('date')
        extra = {}
        for cn, en in (('note', 'note'), ('commission', 'commission'), ('tax', 'tax'),
                       ('备注', 'note'), ('佣金', 'commission'), ('印花税', 'tax')):
            if t.get(cn) not in (None, '') and en not in extra:
                extra[en] = t.get(cn)
        extra['source'] = t.get('source') or 'import_trades'
        return {'code': code, 'name': name, 'ttype': ttype, 'qty': qty,
                'price': price, 'amount': amount, 'tt': (str(tt) if tt else None), 'extra': extra}

    def import_trades(self, trades: List[Dict], update_position: bool = True) -> Dict[str, int]:
        """批量导入成交记录(真实买卖流水)到 trade_records，并按成交更新持仓。

        trades 字段(中英文皆可): code/股票代码(必填)、trade_type/方向(买入/卖出)、
            quantity/数量(必填)、price/价格(必填)、amount/金额、trade_time/成交时间/日期、
            name、note/备注、commission/佣金、tax/印花税。
        update_position=True(默认): 买入→加仓并重算移动加权成本;卖出→减仓(成本不变,清零保留)。
            走现有 add_stock/update_stock,变动记录带上**实际成交时间**。
        Returns: {'imported', 'failed', 'positions_updated', 'errors'}
        """
        import json
        # 1) 标准化 + 按成交时间排序(保证移动加权成本正确)
        norm = []
        failed = 0
        for t in trades:
            n = self._normalize_trade(t)
            if n:
                norm.append(n)
            else:
                failed += 1
        norm.sort(key=lambda x: x['tt'] or '')

        imported = pos_updated = 0
        errors = []
        conn = get_conn()
        cur = conn.cursor()
        try:
            for n in norm:
                try:
                    # 先按时序更新持仓,拿到这笔成交后的持仓快照(数量/成本/增减)
                    pos_q = pos_c = delta = None
                    if update_position:
                        st = self._apply_trade_to_position(n)
                        if st is not None:
                            pos_q, pos_c, delta = st
                            pos_updated += 1
                    # 卖出时顺手算已实现盈亏 = (卖价-成本快照)×数量-佣金-印花税(2026-06-12)
                    pl = None
                    if n['ttype'] == '卖出' and pos_c is not None:
                        try:
                            fees = float(n['extra'].get('commission') or 0) + float(n['extra'].get('tax') or 0)
                            pl = round((n['price'] - float(pos_c)) * n['qty'] - fees, 2)
                        except (TypeError, ValueError):
                            pl = None
                    # 成交记录行同时写入持仓快照 → 一行既是成交又是变动记录
                    cur.execute("""
                        INSERT INTO trade_records
                            (stock_code, stock_name, trade_type, quantity, price, amount,
                             pos_quantity, pos_cost_price, delta_qty, trade_time, extra, profit_loss)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                                COALESCE(%s::timestamptz, NOW()), %s, %s)
                    """, (n['code'], n['name'], n['ttype'], n['qty'], n['price'], n['amount'],
                          pos_q, pos_c, delta, n['tt'], json.dumps(n['extra'], ensure_ascii=False), pl))
                    imported += 1
                except Exception as e:
                    failed += 1
                    if len(errors) < 5:
                        errors.append(str(e))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
        return {'imported': imported, 'failed': failed,
                'positions_updated': pos_updated, 'errors': errors}

    def _apply_trade_to_position(self, n: Dict):
        """把一笔已标准化的成交应用到持仓:买入加权加仓,卖出减仓。
        返回 (pos_quantity, pos_cost_price, delta_qty) = 成交后持仓快照;未改动持仓返回 None。"""
        existing = self.get_stock_by_code(n['code'])
        qty, price, tt = n['qty'], n['price'], n['tt']
        if n['ttype'] == '买入':
            if existing:
                old_q = existing.get('quantity') or 0
                old_c = existing.get('cost_price')
                new_q = old_q + qty
                new_c = round((old_q * float(old_c) + qty * price) / new_q, 4) if (old_c and old_q) else price
                self.update_stock(existing['id'], cost_price=new_c, quantity=new_q,
                                  source='import_trades', trade_time=tt, log_change=False)
                return (new_q, new_c, new_q - old_q)
            else:
                self.add_stock(n['code'], n['name'] or n['code'], cost_price=price,
                               quantity=qty, source='import_trades', trade_time=tt, log_change=False)
                return (qty, price, qty)
        else:  # 卖出
            if existing:
                old_q = existing.get('quantity') or 0
                old_c = existing.get('cost_price')
                new_q = max(0, old_q - qty)
                self.update_stock(existing['id'], quantity=new_q,
                                  source='import_trades', trade_time=tt, log_change=False)
                return (new_q, (float(old_c) if old_c else None), new_q - old_q)
            return None  # 未持有该股的卖出,只记流水不动持仓(pos_* 留空)

    def get_trades(self, code: Optional[str] = None, limit: int = 200) -> List[Dict]:
        """查询成交记录(真实买卖,trade_type in 买入/卖出),按时间倒序。"""
        conn = get_conn()
        cur = conn.cursor()
        try:
            sel = ("stock_code, stock_name, trade_type, quantity, price, amount, "
                   "pos_quantity, pos_cost_price, delta_qty, trade_time, extra")
            if code:
                cur.execute(f"""SELECT {sel} FROM trade_records
                               WHERE stock_code=%s AND trade_type IN ('买入','卖出')
                               ORDER BY trade_time DESC LIMIT %s""", (code, limit))
            else:
                cur.execute(f"""SELECT {sel} FROM trade_records WHERE trade_type IN ('买入','卖出')
                               ORDER BY trade_time DESC LIMIT %s""", (limit,))
            cols = ['code', 'name', 'trade_type', 'quantity', 'price', 'amount',
                    'pos_quantity', 'pos_cost_price', 'delta_qty', 'trade_time', 'extra']
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    # ==================== 变动历史查询 ====================

    def get_change_history(self, code: Optional[str] = None,
                            change_type: Optional[str] = None,
                            since_days: int = 90,
                            limit: int = 200) -> List[Dict]:
        """查询持仓变动历史

        Args:
            code: 股票代码筛选
            change_type: add/update/delete/bulk_import 等
            since_days: 最近 N 天
            limit: 返回条数上限
        """
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            # 变动日志已并入合并表 trade_records(变动行 + 成交行统一时间线)
            where = ["trade_time >= NOW() - (%s || ' days')::interval"]
            params = [since_days]
            if code:
                where.append("stock_code = %s")
                params.append(code)
            if change_type:
                where.append("trade_type = %s")
                params.append(change_type)
            sql = f"""
                SELECT id, stock_code AS code, stock_name AS name,
                       trade_type AS change_type, delta_qty,
                       pos_quantity AS quantity, pos_cost_price AS cost_price,
                       price, amount, source, note, trade_time AS changed_at
                FROM trade_records
                WHERE {' AND '.join(where)}
                ORDER BY trade_time DESC
                LIMIT %s
            """
            params.append(limit)
            cur.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def get_change_stats(self, since_days: int = 90) -> Dict:
        """变动统计：交易频次、买卖比、活跃股票"""
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT trade_type, COUNT(*) FROM trade_records
                WHERE trade_time >= NOW() - (%s || ' days')::interval
                GROUP BY trade_type
            """, (since_days,))
            by_type = dict(cur.fetchall())

            cur.execute("""
                SELECT stock_code, stock_name, COUNT(*) AS cnt FROM trade_records
                WHERE trade_time >= NOW() - (%s || ' days')::interval
                GROUP BY stock_code, stock_name ORDER BY cnt DESC LIMIT 10
            """, (since_days,))
            most_active = [{'code': r[0], 'name': r[1], 'count': r[2]} for r in cur.fetchall()]

            return {'by_type': by_type, 'most_active': most_active, 'since_days': since_days}
        finally:
            cur.close()
            conn.close()

    # ==================== 分析历史 ====================

    def save_analysis(self, stock_id: int, rating: str, confidence: float,
                      current_price: float, target_price: Optional[float] = None,
                      entry_min: Optional[float] = None,
                      entry_max: Optional[float] = None,
                      take_profit: Optional[float] = None,
                      stop_loss: Optional[float] = None,
                      summary: str = "") -> int:
        try:
            from enums import normalize_rating
            rating = normalize_rating(rating) if rating else rating
        except Exception:
            pass
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO portfolio_analysis_history
                    (portfolio_stock_id, analysis_time, rating, confidence,
                     current_price, target_price, entry_min, entry_max,
                     take_profit, stop_loss, summary)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (stock_id, datetime.now(), rating, confidence, current_price,
                  target_price, entry_min, entry_max, take_profit, stop_loss, summary))
            aid = cur.fetchone()[0]
            conn.commit()
            return aid
        finally:
            cur.close()
            conn.close()

    def get_analysis_history(self, stock_id: int, limit: int = 10) -> List[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""
                SELECT * FROM portfolio_analysis_history
                WHERE portfolio_stock_id = %s
                ORDER BY analysis_time DESC
                LIMIT %s
            """, (stock_id, limit))
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def get_latest_analysis(self, stock_id: int) -> Optional[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""
                SELECT * FROM portfolio_analysis_history
                WHERE portfolio_stock_id = %s
                ORDER BY analysis_time DESC
                LIMIT 1
            """, (stock_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            cur.close()
            conn.close()

    def get_all_latest_analysis(self) -> List[Dict]:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute("""
                SELECT DISTINCT ON (p.id)
                    p.*, h.rating, h.confidence, h.current_price, h.target_price,
                    h.entry_min, h.entry_max, h.take_profit, h.stop_loss,
                    h.analysis_time
                FROM portfolio_stocks p
                LEFT JOIN portfolio_analysis_history h
                    ON p.id = h.portfolio_stock_id
                ORDER BY p.id, h.analysis_time DESC
            """)
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

    def get_latest_analysis_history(self, stock_id: int, limit: int = 10) -> List[Dict]:
        return self.get_analysis_history(stock_id, limit)

    def delete_old_analysis(self, days: int = 90) -> int:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                DELETE FROM portfolio_analysis_history
                WHERE analysis_time < CURRENT_TIMESTAMP - INTERVAL '%s days'
            """, (days,))
            conn.commit()
            return cur.rowcount
        finally:
            cur.close()
            conn.close()

    def get_rating_changes(self, stock_id: int, days: int = 30) -> List[Tuple]:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT analysis_time, rating
                FROM portfolio_analysis_history
                WHERE portfolio_stock_id = %s
                  AND analysis_time >= CURRENT_TIMESTAMP - INTERVAL '%s days'
                ORDER BY analysis_time ASC
            """, (stock_id, days))
            rows = cur.fetchall()
            changes = []
            for i in range(1, len(rows)):
                if rows[i][1] != rows[i - 1][1]:
                    changes.append((str(rows[i][0]), rows[i - 1][1], rows[i][1]))
            return changes
        finally:
            cur.close()
            conn.close()

    # ==================== 统计 ====================

    def get_portfolio_stats(self) -> Dict:
        """获取持仓总览统计"""
        conn = get_conn()
        cur = conn.cursor()
        try:
            stats = {}
            cur.execute("SELECT COUNT(*) FROM portfolio_stocks")
            stats["stock_count"] = cur.fetchone()[0]

            cur.execute("""
                SELECT SUM(cost_price * quantity) AS total_cost,
                       SUM(quantity) AS total_shares
                FROM portfolio_stocks
                WHERE cost_price IS NOT NULL AND quantity IS NOT NULL
            """)
            row = cur.fetchone()
            stats["total_cost"] = float(row[0]) if row and row[0] else 0
            stats["total_shares"] = int(row[1]) if row and row[1] else 0

            cur.execute("""
                SELECT h.rating, COUNT(*) AS cnt
                FROM portfolio_stocks p
                JOIN LATERAL (
                    SELECT rating FROM portfolio_analysis_history
                    WHERE portfolio_stock_id = p.id
                    ORDER BY analysis_time DESC LIMIT 1
                ) h ON TRUE
                GROUP BY h.rating
                ORDER BY cnt DESC
            """)
            stats["rating_distribution"] = {r[0]: r[1] for r in cur.fetchall()}

            return stats
        finally:
            cur.close()
            conn.close()


# 全局实例
portfolio_db = PortfolioDBPG()


if __name__ == "__main__":
    sep = "=" * 55
    print(sep)
    print("  PostgreSQL 持仓模块 — 自检")
    print(sep)

    # 总览
    print(f"\n  持仓: {portfolio_db.get_stock_count()} 只")

    stats = portfolio_db.get_portfolio_stats()
    print(f"  总成本: {stats['total_cost']:.2f}")
    print(f"  总股数: {stats['total_shares']}")
    print(f"  评级分布: {stats.get('rating_distribution', {})}")

    print(f"\n  持仓列表:")
    for s in portfolio_db.get_all_stocks():
        last = portfolio_db.get_latest_analysis(s['id'])
        rating = last['rating'] if last else 'N/A'
        total = (s['cost_price'] or 0) * (s['quantity'] or 0)
        print(f"    {s['code']:>8} {s['name']:<8} 成本:{s['cost_price']:>8.2f} 数量:{s['quantity']:>5} 总额:{total:>8.2f} 评级:{rating}")

    print(f"\n{sep}")
    print('  ✅ 引用方式: from portfolio_db_pg import portfolio_db')
    print(sep)
