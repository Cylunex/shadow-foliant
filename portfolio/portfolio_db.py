import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""
持仓股票数据库管理模块

提供持仓股票和分析历史的数据库操作接口
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import os

# 确保 .env 在切换 PG/SQLite 之前已加载
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 数据库文件路径
DB_PATH = _bootstrap.db_path("portfolio_stocks.db")


class PortfolioDB:
    """持仓股票数据库管理类"""
    
    def __init__(self, db_path: str = DB_PATH):
        """
        初始化数据库连接
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        self._init_database()
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使查询结果可以通过列名访问
        return conn
    
    def _init_database(self):
        """初始化数据库表结构"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 创建持仓股票表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS portfolio_stocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    cost_price REAL,
                    quantity INTEGER,
                    note TEXT,
                    auto_monitor BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建持仓分析历史表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS portfolio_analysis_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portfolio_stock_id INTEGER NOT NULL,
                    analysis_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    rating TEXT,
                    confidence REAL,
                    current_price REAL,
                    target_price REAL,
                    entry_min REAL,
                    entry_max REAL,
                    take_profit REAL,
                    stop_loss REAL,
                    summary TEXT,
                    FOREIGN KEY (portfolio_stock_id) REFERENCES portfolio_stocks(id) ON DELETE CASCADE
                )
            ''')
            
            # 创建索引以提升查询性能
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_portfolio_analysis_stock_id 
                ON portfolio_analysis_history(portfolio_stock_id)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_portfolio_analysis_time 
                ON portfolio_analysis_history(analysis_time DESC)
            ''')
            
            conn.commit()
            print(f"[OK] 数据库初始化成功: {self.db_path}")
            
        except Exception as e:
            print(f"[ERROR] 数据库初始化失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    # ==================== 持仓股票CRUD操作 ====================
    
    def add_stock(self, code: str, name: str, cost_price: Optional[float] = None,
                  quantity: Optional[int] = None, note: str = "", 
                  auto_monitor: bool = True) -> int:
        """
        添加持仓股票
        
        Args:
            code: 股票代码
            name: 股票名称
            cost_price: 持仓成本价（可选）
            quantity: 持仓数量（可选）
            note: 备注信息
            auto_monitor: 是否自动同步到监测列表
            
        Returns:
            新增股票的ID
            
        Raises:
            sqlite3.IntegrityError: 如果股票代码已存在
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO portfolio_stocks 
                (code, name, cost_price, quantity, note, auto_monitor, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, name, cost_price, quantity, note, auto_monitor, 
                  datetime.now(), datetime.now()))
            
            conn.commit()
            stock_id = cursor.lastrowid
            print(f"[OK] 添加持仓股票成功: {code} {name} (ID: {stock_id})")
            return stock_id
            
        except sqlite3.IntegrityError as e:
            print(f"[ERROR] 股票代码已存在: {code}")
            raise ValueError(f"股票代码 {code} 已存在") from e
        except Exception as e:
            print(f"[ERROR] 添加持仓股票失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def update_stock(self, stock_id: int, **kwargs) -> bool:
        """
        更新持仓股票信息
        
        Args:
            stock_id: 股票ID
            **kwargs: 要更新的字段（code, name, cost_price, quantity, note, auto_monitor）
            
        Returns:
            是否更新成功
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 允许更新的字段
        allowed_fields = ['code', 'name', 'cost_price', 'quantity', 'note', 'auto_monitor']
        update_fields = {k: v for k, v in kwargs.items() if k in allowed_fields}
        
        if not update_fields:
            print("[WARN] 没有需要更新的字段")
            return False
        
        # 添加更新时间
        update_fields['updated_at'] = datetime.now()
        
        # 构建SQL语句
        set_clause = ', '.join([f"{field} = ?" for field in update_fields.keys()])
        values = list(update_fields.values()) + [stock_id]
        
        try:
            cursor.execute(f'''
                UPDATE portfolio_stocks 
                SET {set_clause}
                WHERE id = ?
            ''', values)
            
            conn.commit()
            
            if cursor.rowcount > 0:
                print(f"[OK] 更新持仓股票成功: ID {stock_id}")
                return True
            else:
                print(f"[WARN] 未找到股票: ID {stock_id}")
                return False
                
        except Exception as e:
            print(f"[ERROR] 更新持仓股票失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def delete_stock(self, stock_id: int) -> bool:
        """
        删除持仓股票（级联删除其所有分析历史）
        
        Args:
            stock_id: 股票ID
            
        Returns:
            是否删除成功
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 由于设置了ON DELETE CASCADE，删除股票会自动删除其分析历史
            cursor.execute('DELETE FROM portfolio_stocks WHERE id = ?', (stock_id,))
            conn.commit()
            
            if cursor.rowcount > 0:
                print(f"[OK] 删除持仓股票成功: ID {stock_id}")
                return True
            else:
                print(f"[WARN] 未找到股票: ID {stock_id}")
                return False
                
        except Exception as e:
            print(f"[ERROR] 删除持仓股票失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_stock(self, stock_id: int) -> Optional[Dict]:
        """
        获取单只持仓股票信息
        
        Args:
            stock_id: 股票ID
            
        Returns:
            股票信息字典，不存在则返回None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT * FROM portfolio_stocks WHERE id = ?', (stock_id,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
            
        finally:
            conn.close()
    
    def get_stock_by_code(self, code: str) -> Optional[Dict]:
        """
        根据股票代码获取持仓股票信息
        
        Args:
            code: 股票代码
            
        Returns:
            股票信息字典，不存在则返回None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT * FROM portfolio_stocks WHERE code = ?', (code,))
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            return None
            
        finally:
            conn.close()
    
    def get_all_stocks(self, auto_monitor_only: bool = False,
                       include_cleared: bool = False) -> List[Dict]:
        """
        获取所有持仓股票列表

        Args:
            auto_monitor_only: 是否只返回启用自动监测的股票
            include_cleared: 是否包含 quantity=0 的清仓记录(默认 False, 即不返回)
                ⭐ 2026-06-17: 默认过滤清仓行(quantity=0), 此前所有分析任务
                (持仓分析/监控/AI 研判)还在跑已清仓的股票, 浪费资源 + 误报。
                quantity IS NULL 视为"未填数量"(用代码记账的用户), 仍保留。
                显式传 include_cleared=True 才返回全部(交易历史/汇总等场景)。

        Returns:
            股票信息字典列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            where = []
            if auto_monitor_only:
                where.append('auto_monitor = 1')
            if not include_cleared:
                # 排除明确为 0 的清仓行; NULL(未填)保留
                where.append('(quantity IS NULL OR quantity != 0)')
            sql = 'SELECT * FROM portfolio_stocks'
            if where:
                sql += ' WHERE ' + ' AND '.join(where)
            sql += ' ORDER BY created_at DESC'
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        finally:
            conn.close()
    
    def search_stocks(self, keyword: str) -> List[Dict]:
        """
        搜索持仓股票（按代码或名称）
        
        Args:
            keyword: 搜索关键词
            
        Returns:
            匹配的股票信息字典列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            keyword_pattern = f"%{keyword}%"
            cursor.execute('''
                SELECT * FROM portfolio_stocks 
                WHERE code LIKE ? OR name LIKE ?
                ORDER BY created_at DESC
            ''', (keyword_pattern, keyword_pattern))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        finally:
            conn.close()
    
    def get_stock_count(self) -> int:
        """
        获取持仓股票总数
        
        Returns:
            股票数量
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('SELECT COUNT(*) as count FROM portfolio_stocks')
            result = cursor.fetchone()
            return result['count']
            
        finally:
            conn.close()
    
    # ==================== 分析历史记录操作 ====================
    
    def save_analysis(self, stock_id: int, rating: str, confidence: float,
                     current_price: float, target_price: Optional[float] = None,
                     entry_min: Optional[float] = None, entry_max: Optional[float] = None,
                     take_profit: Optional[float] = None, stop_loss: Optional[float] = None,
                     summary: str = "") -> int:
        """
        保存分析历史记录
        
        Args:
            stock_id: 持仓股票ID
            rating: 投资评级（买入/持有/卖出）
            confidence: 信心度（0-10）
            current_price: 当前价格
            target_price: 目标价位
            entry_min: 进场区间最小值
            entry_max: 进场区间最大值
            take_profit: 止盈位
            stop_loss: 止损位
            summary: 分析摘要
            
        Returns:
            新增分析记录的ID
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO portfolio_analysis_history 
                (portfolio_stock_id, analysis_time, rating, confidence, current_price,
                 target_price, entry_min, entry_max, take_profit, stop_loss, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (stock_id, datetime.now(), rating, confidence, current_price,
                  target_price, entry_min, entry_max, take_profit, stop_loss, summary))
            
            conn.commit()
            analysis_id = cursor.lastrowid
            print(f"[OK] 保存分析历史成功: 股票ID {stock_id}, 评级 {rating}")
            return analysis_id
            
        except Exception as e:
            print(f"[ERROR] 保存分析历史失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_analysis_history(self, stock_id: int, limit: int = 10) -> List[Dict]:
        """
        获取股票的分析历史记录
        
        Args:
            stock_id: 持仓股票ID
            limit: 返回记录数量限制
            
        Returns:
            分析历史记录列表（按时间倒序）
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT * FROM portfolio_analysis_history 
                WHERE portfolio_stock_id = ?
                ORDER BY analysis_time DESC
                LIMIT ?
            ''', (stock_id, limit))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        finally:
            conn.close()
    
    def get_latest_analysis_history(self, stock_id: int, limit: int = 10) -> List[Dict]:
        """
        获取股票的最新分析历史记录（按时间倒序）
        
        这是 get_analysis_history 的别名方法，用于保持代码兼容性
        
        Args:
            stock_id: 持仓股票ID
            limit: 返回记录数量限制
            
        Returns:
            分析历史记录列表（按时间倒序）
        """
        return self.get_analysis_history(stock_id, limit)
    
    def get_latest_analysis(self, stock_id: int) -> Optional[Dict]:
        """
        获取股票的最新一次分析记录
        
        Args:
            stock_id: 持仓股票ID
            
        Returns:
            最新分析记录字典，不存在则返回None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT * FROM portfolio_analysis_history 
                WHERE portfolio_stock_id = ?
                ORDER BY analysis_time DESC
                LIMIT 1
            ''', (stock_id,))
            
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
            
        finally:
            conn.close()
    
    def get_rating_changes(self, stock_id: int, days: int = 30) -> List[Tuple[str, str, str]]:
        """
        获取股票在指定天数内的评级变化
        
        Args:
            stock_id: 持仓股票ID
            days: 查询天数
            
        Returns:
            评级变化列表 [(时间, 旧评级, 新评级), ...]
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT analysis_time, rating 
                FROM portfolio_analysis_history 
                WHERE portfolio_stock_id = ?
                AND analysis_time >= datetime('now', '-' || ? || ' days')
                ORDER BY analysis_time ASC
            ''', (stock_id, days))
            
            rows = cursor.fetchall()
            
            changes = []
            for i in range(1, len(rows)):
                prev_rating = rows[i-1]['rating']
                curr_rating = rows[i]['rating']
                if prev_rating != curr_rating:
                    changes.append((
                        rows[i]['analysis_time'],
                        prev_rating,
                        curr_rating
                    ))
            
            return changes
            
        finally:
            conn.close()
    
    def delete_old_analysis(self, days: int = 90) -> int:
        """
        删除超过指定天数的分析历史记录
        
        Args:
            days: 保留天数
            
        Returns:
            删除的记录数量
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                DELETE FROM portfolio_analysis_history 
                WHERE analysis_time < datetime('now', '-' || ? || ' days')
            ''', (days,))
            
            conn.commit()
            deleted_count = cursor.rowcount
            print(f"[OK] 清理历史分析记录: 删除 {deleted_count} 条记录")
            return deleted_count
            
        except Exception as e:
            print(f"[ERROR] 清理历史分析记录失败: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_all_latest_analysis(self, include_cleared: bool = False) -> List[Dict]:
        """
        获取所有持仓股票的最新分析记录。
        include_cleared=False(默认)过滤 quantity=0 的清仓行,与 get_all_stocks 一致 ——
        否则周报/持仓页会带出已清仓股票的陈旧分析(2026-06-28 修)。

        Returns:
            包含股票信息和最新分析的字典列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        where = '' if include_cleared else 'WHERE (s.quantity IS NULL OR s.quantity != 0)'

        try:
            cursor.execute(f'''
                SELECT
                    s.*,
                    h.rating, h.confidence, h.current_price, h.target_price,
                    h.entry_min, h.entry_max, h.take_profit, h.stop_loss,
                    h.analysis_time
                FROM portfolio_stocks s
                LEFT JOIN (
                    SELECT h1.*
                    FROM portfolio_analysis_history h1
                    INNER JOIN (
                        SELECT portfolio_stock_id, MAX(analysis_time) as max_time
                        FROM portfolio_analysis_history
                        GROUP BY portfolio_stock_id
                    ) h2
                    ON h1.portfolio_stock_id = h2.portfolio_stock_id
                    AND h1.analysis_time = h2.max_time
                ) h ON s.id = h.portfolio_stock_id
                {where}
                ORDER BY s.created_at DESC
            ''')

            rows = cursor.fetchall()
            return [dict(row) for row in rows]

        finally:
            conn.close()

    # ==================== 批量导入（SQLite 简易版）====================

    def bulk_import(self, stocks: List[Dict], mode: str = 'upsert',
                    source: str = 'bulk_import') -> Dict[str, int]:
        """批量导入 — SQLite 简易实现（无变动表，仅写主表）

        生产建议使用 PG 模式（USE_POSTGRES=true）以获得完整变动记录
        """
        added = updated = skipped = deleted = 0
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            if mode == 'replace':
                cur.execute('SELECT COUNT(*) FROM portfolio_stocks')
                deleted = cur.fetchone()[0]
                cur.execute('DELETE FROM portfolio_stocks')

            for s in stocks:
                code = s.get('code') or s.get('股票代码') or s.get('symbol')
                if not code:
                    skipped += 1
                    continue
                code = str(code).strip()
                if code.isdigit() and len(code) <= 6:
                    code = code.zfill(6)
                name = s.get('name') or s.get('股票名称') or s.get('股票简称') or code
                cost_price = s.get('cost_price') or s.get('成本价')
                quantity = s.get('quantity') or s.get('数量')
                note = s.get('note') or s.get('备注') or ''
                try:
                    cost_price = float(cost_price) if cost_price not in (None, '') else None
                except (TypeError, ValueError):
                    cost_price = None
                try:
                    quantity = int(quantity) if quantity not in (None, '') else None
                except (TypeError, ValueError):
                    quantity = None

                cur.execute('SELECT id FROM portfolio_stocks WHERE code = ?', (code,))
                existing = cur.fetchone()
                if existing:
                    if mode == 'add':
                        skipped += 1
                        continue
                    cur.execute('''
                        UPDATE portfolio_stocks
                        SET name=?, cost_price=?, quantity=?, note=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE code=?
                    ''', (name, cost_price, quantity, note, code))
                    updated += 1
                else:
                    cur.execute('''
                        INSERT INTO portfolio_stocks
                            (code, name, cost_price, quantity, note, auto_monitor)
                        VALUES (?, ?, ?, ?, ?, 1)
                    ''', (code, name, cost_price, quantity, note))
                    added += 1
            conn.commit()
        finally:
            conn.close()
        return {'added': added, 'updated': updated, 'skipped': skipped, 'deleted': deleted}

    # ==================== 批量导入成交记录（SQLite）====================

    def import_trades(self, trades: List[Dict], update_position: bool = True) -> Dict[str, int]:
        """批量导入成交记录(真实买卖流水)。SQLite 模式自建 trade_records 表。

        字段同 PG 版:code/数量 quantity/价格 price 必填,trade_type(买入/卖出)、
        amount、trade_time、note、commission、tax 可选。
        update_position=True(默认):按成交时间排序后,买入→加权加仓、卖出→减仓,
            并把成交后持仓快照(pos_quantity/pos_cost_price/delta_qty)写入该成交行
            (行为对齐 PG 版;在同一连接内更新 portfolio_stocks,避免 SQLite 写锁冲突)。
        """
        import json
        SELL = {'卖出', '卖', 'sell', 's', 'S', '减仓'}
        # 1) 标准化
        norm = []
        failed = 0
        for t in trades:
            code = t.get('code') or t.get('股票代码') or t.get('symbol')
            qty = t.get('quantity') or t.get('数量') or t.get('qty')
            price = t.get('price') or t.get('价格') or t.get('成交价')
            if not code or qty in (None, '') or price in (None, ''):
                failed += 1
                continue
            code = str(code).strip()
            if code.isdigit() and len(code) <= 6:
                code = code.zfill(6)
            name = t.get('name') or t.get('股票名称') or t.get('股票简称')
            raw = str(t.get('trade_type') or t.get('方向') or t.get('direction') or '买入').strip()
            ttype = '卖出' if raw in SELL else '买入'
            qty = int(float(qty)); price = float(price)
            amount = t.get('amount') or t.get('金额')
            amount = float(amount) if amount not in (None, '') else round(qty * price, 2)
            tt = t.get('trade_time') or t.get('成交时间') or t.get('日期') or t.get('date')
            extra = {k: t.get(k) for k in ('note', 'commission', 'tax') if t.get(k) not in (None, '')}
            for cn, en in (('备注', 'note'), ('佣金', 'commission'), ('印花税', 'tax')):
                if t.get(cn) not in (None, '') and en not in extra:
                    extra[en] = t.get(cn)
            norm.append({'code': code, 'name': name, 'ttype': ttype, 'qty': qty, 'price': price,
                         'amount': amount, 'tt': (str(tt) if tt else None), 'extra': extra})
        # 2) 按成交时间排序(保证移动加权成本正确)
        norm.sort(key=lambda x: x['tt'] or '')

        imported = pos_updated = 0
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stock_code TEXT NOT NULL, stock_name TEXT, trade_type TEXT,
                    quantity INTEGER, price REAL, amount REAL,
                    pos_quantity INTEGER, pos_cost_price REAL, delta_qty INTEGER,
                    trade_time TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, extra TEXT)
            ''')
            for n in norm:
                try:
                    # 先按时序更新持仓(同一连接),拿到成交后快照(数量/成本/增减)
                    pos_q = pos_c = delta = None
                    if update_position:
                        st = self._apply_trade_to_position_sqlite(cur, n)
                        if st is not None:
                            pos_q, pos_c, delta = st
                            pos_updated += 1
                    cur.execute('''INSERT INTO trade_records
                        (stock_code, stock_name, trade_type, quantity, price, amount,
                         pos_quantity, pos_cost_price, delta_qty, trade_time, extra)
                        VALUES (?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP),?)''',
                        (n['code'], n['name'], n['ttype'], n['qty'], n['price'], n['amount'],
                         pos_q, pos_c, delta, n['tt'], json.dumps(n['extra'], ensure_ascii=False)))
                    imported += 1
                except Exception:
                    failed += 1
            conn.commit()
        finally:
            conn.close()
        return {'imported': imported, 'failed': failed, 'positions_updated': pos_updated}

    def _apply_trade_to_position_sqlite(self, cur, n: Dict):
        """把一笔已标准化的成交应用到持仓(SQLite,复用传入 cursor 同事务):
        买入→加权加仓,卖出→减仓。返回 (pos_quantity, pos_cost_price, delta_qty);未改动返回 None。"""
        cur.execute("SELECT id, cost_price, quantity FROM portfolio_stocks WHERE code = ?", (n['code'],))
        row = cur.fetchone()
        qty, price = n['qty'], n['price']
        if n['ttype'] == '买入':
            if row:
                old_q = row['quantity'] or 0
                old_c = row['cost_price']
                new_q = old_q + qty
                new_c = round((old_q * float(old_c) + qty * price) / new_q, 4) if (old_c and old_q) else price
                cur.execute("UPDATE portfolio_stocks SET cost_price=?, quantity=?, updated_at=? WHERE id=?",
                            (new_c, new_q, datetime.now(), row['id']))
                return (new_q, new_c, new_q - old_q)
            else:
                cur.execute('''INSERT INTO portfolio_stocks
                    (code, name, cost_price, quantity, note, auto_monitor, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (n['code'], n['name'] or n['code'], price, qty, '', 1, datetime.now(), datetime.now()))
                return (qty, price, qty)
        else:  # 卖出
            if row:
                old_q = row['quantity'] or 0
                old_c = row['cost_price']
                new_q = max(0, old_q - qty)
                cur.execute("UPDATE portfolio_stocks SET quantity=?, updated_at=? WHERE id=?",
                            (new_q, datetime.now(), row['id']))
                return (new_q, (float(old_c) if old_c else None), new_q - old_q)
            return None  # 未持有该股的卖出,只记流水不动持仓

    def get_trades(self, code=None, limit: int = 200) -> List[Dict]:
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_records'")
            if not cur.fetchone():
                return []
            sel = ("stock_code, stock_name, trade_type, quantity, price, amount, "
                   "pos_quantity, pos_cost_price, delta_qty, trade_time, extra")
            if code:
                cur.execute(f"""SELECT {sel} FROM trade_records WHERE stock_code=?
                               ORDER BY trade_time DESC LIMIT ?""", (code, limit))
            else:
                cur.execute(f"""SELECT {sel} FROM trade_records ORDER BY trade_time DESC LIMIT ?""", (limit,))
            cols = ['code', 'name', 'trade_type', 'quantity', 'price', 'amount',
                    'pos_quantity', 'pos_cost_price', 'delta_qty', 'trade_time', 'extra']
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_change_history(self, code=None, change_type=None,
                           since_days=90, limit=200):
        """SQLite 模式无变动表，返回空（PG 模式才有完整记录）"""
        return []

    def get_change_stats(self, since_days=90):
        return {'by_type': {}, 'most_active': [], 'since_days': since_days,
                'note': '变动记录仅 PG 模式可用'}


# 创建全局数据库实例 — 通过 .env 的 USE_POSTGRES=true 切换到 PostgreSQL 后端
if os.getenv('USE_POSTGRES', '').lower() in ('1', 'true', 'yes', 'on'):
    try:
        from portfolio_db_pg import portfolio_db as _pg_pdb
        portfolio_db = _pg_pdb
        print('[Portfolio] 已切换到 PostgreSQL 后端 (portfolio_stocks)')
    except Exception as _e:
        print(f'[Portfolio] PG 切换失败，回退到 SQLite: {_e}')
        portfolio_db = PortfolioDB()
else:
    portfolio_db = PortfolioDB()


if __name__ == "__main__":
    # 测试代码
    print("=" * 50)
    print("持仓股票数据库测试")
    print("=" * 50)
    
    # 初始化数据库
    db = PortfolioDB("test_portfolio.db")
    
    # 测试添加股票
    try:
        stock_id = db.add_stock("600519", "贵州茅台", 1650.5, 100, "长期持有")
        print(f"\n添加股票ID: {stock_id}")
    except ValueError as e:
        print(f"\n{e}")
    
    # 测试查询所有股票
    print("\n所有持仓股票:")
    stocks = db.get_all_stocks()
    for stock in stocks:
        print(f"  {stock['code']} {stock['name']}")
    
    # 测试保存分析历史
    if stocks:
        stock_id = stocks[0]['id']
        analysis_id = db.save_analysis(
            stock_id, "买入", 8.5, 1700.0, 1850.0,
            1600.0, 1650.0, 1900.0, 1500.0,
            "技术面和基本面均良好"
        )
        print(f"\n保存分析记录ID: {analysis_id}")
        
        # 查询分析历史
        print(f"\n股票 {stocks[0]['code']} 的分析历史:")
        history = db.get_analysis_history(stock_id)
        for h in history:
            print(f"  {h['analysis_time']}: {h['rating']} (信心度: {h['confidence']})")
    
    print("\n[OK] 数据库测试完成")

