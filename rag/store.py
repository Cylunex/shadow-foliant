"""pgvector 向量库 —— doc_embeddings 表 + upsert + 余弦检索。优雅降级(PG/pgvector 挂了 no-op/[])。

向量以字符串 '[v1,v2,...]'::vector 传参,免装 pgvector python 包(零额外依赖)。
仅 PG 后端有效(USE_POSTGRES=true);SQLite 环境直接降级(检索功能停用,不影响主功能)。
"""

from __future__ import annotations

import json
import os
import time
from typing import List, Dict, Optional

from embed_client import DIM

_down_until = 0.0
_schema_ready = False
_warned = False


def _conn():
    """返回 PG 连接;不可用返回 None(冷却 30s)。"""
    global _down_until, _warned
    if _down_until and time.time() < _down_until:
        return None
    if os.getenv('USE_POSTGRES', '').lower() not in ('1', 'true', 'yes', 'on'):
        return None
    try:
        import psycopg2
        return psycopg2.connect(
            host=os.getenv('PG_HOST', '127.0.0.1'), port=int(os.getenv('PG_PORT', '5432')),
            dbname=os.getenv('PG_DATABASE'), user=os.getenv('PG_USER'),
            password=os.getenv('PG_PASSWORD'), connect_timeout=8)
    except Exception as e:
        global _warned
        if not _warned:
            print(f'[rag.store] PG 不可用,向量检索降级: {type(e).__name__}')
            _warned = True
        _down_until = time.time() + 30
        return None


def _vec_literal(vec: List[float]) -> str:
    return '[' + ','.join(f'{float(x):.7g}' for x in vec) + ']'


def ensure_schema() -> bool:
    """建扩展/表/索引(幂等)。成功 True。"""
    global _schema_ready
    if _schema_ready:
        return True
    conn = _conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS doc_embeddings (
            id BIGSERIAL PRIMARY KEY,
            source_type TEXT NOT NULL,
            ref_id      TEXT NOT NULL,
            title       TEXT,
            content     TEXT,
            meta        JSONB,
            embedding   vector({DIM}),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (source_type, ref_id))""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_doc_emb_hnsw "
                    "ON doc_embeddings USING hnsw (embedding vector_cosine_ops)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_doc_emb_src ON doc_embeddings (source_type)")
        conn.commit()
        _schema_ready = True
        return True
    except Exception as e:
        conn.rollback()
        print(f'[rag.store] 建表失败: {type(e).__name__}: {e}')
        return False
    finally:
        conn.close()


def upsert(items: List[Dict]) -> int:
    """items: [{source_type, ref_id, title, content, meta, embedding}]。返回写入条数。失败 0。"""
    if not items or not ensure_schema():
        return 0
    conn = _conn()
    if not conn:
        return 0
    n = 0
    try:
        cur = conn.cursor()
        for it in items:
            emb = it.get('embedding')
            if not emb:
                continue
            cur.execute("""INSERT INTO doc_embeddings (source_type, ref_id, title, content, meta, embedding)
                VALUES (%s,%s,%s,%s,%s,%s::vector)
                ON CONFLICT (source_type, ref_id) DO UPDATE SET
                    title=EXCLUDED.title, content=EXCLUDED.content,
                    meta=EXCLUDED.meta, embedding=EXCLUDED.embedding, created_at=NOW()""",
                (it['source_type'], str(it['ref_id']), it.get('title'),
                 (it.get('content') or '')[:8000],
                 json.dumps(it.get('meta') or {}, ensure_ascii=False), _vec_literal(emb)))
            n += 1
        conn.commit()
        return n
    except Exception as e:
        conn.rollback()
        print(f'[rag.store] upsert 失败: {type(e).__name__}: {e}')
        return 0
    finally:
        conn.close()


def search(query_vec: List[float], top_n: int = 40,
           source_types: Optional[List[str]] = None) -> List[Dict]:
    """余弦最近邻。返回 [{source_type,ref_id,title,content,meta,distance}]。失败 []。"""
    if not query_vec or not ensure_schema():
        return []
    conn = _conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        qv = _vec_literal(query_vec)
        if source_types:
            cur.execute("""SELECT source_type, ref_id, title, content, meta,
                              embedding <=> %s::vector AS distance
                            FROM doc_embeddings WHERE source_type = ANY(%s)
                            ORDER BY embedding <=> %s::vector LIMIT %s""",
                        (qv, list(source_types), qv, top_n))
        else:
            cur.execute("""SELECT source_type, ref_id, title, content, meta,
                              embedding <=> %s::vector AS distance
                            FROM doc_embeddings
                            ORDER BY embedding <=> %s::vector LIMIT %s""",
                        (qv, qv, top_n))
        cols = ['source_type', 'ref_id', 'title', 'content', 'meta', 'distance']
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        print(f'[rag.store] 检索失败: {type(e).__name__}: {e}')
        return []
    finally:
        conn.close()


def stats() -> Dict:
    """库存统计(给设置/诊断页)。"""
    if not ensure_schema():
        return {'available': False}
    conn = _conn()
    if not conn:
        return {'available': False}
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_type, count(*) FROM doc_embeddings GROUP BY 1 ORDER BY 2 DESC")
        by = dict(cur.fetchall())
        return {'available': True, 'total': sum(by.values()), 'by_source': by}
    except Exception:
        return {'available': False}
    finally:
        conn.close()


if __name__ == '__main__':
    import sys, io, os as _os
    sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    import _bootstrap  # noqa
    from dotenv import load_dotenv
    load_dotenv()
    print('schema:', ensure_schema())
    from embed_client import embed
    vs = embed(['茅台一季度净利增长20%', '平安银行不良率下降', '宁德时代储能订单大增'])
    if vs:
        items = [{'source_type': 'demo', 'ref_id': str(i), 'title': t, 'content': t,
                  'meta': {'i': i}, 'embedding': v} for i, (t, v) in
                 enumerate(zip(['茅台一季度净利增长20%', '平安银行不良率下降', '宁德时代储能订单大增'], vs))]
        print('upsert:', upsert(items))
        qv = embed(['白酒龙头业绩'])[0]
        for h in search(qv, top_n=3):
            print(f"  dist={h['distance']:.3f}  {h['title']}")
    print('stats:', stats())
