"""RAG 服务 —— 语义检索(embed→pgvector→rerank)+ 多源摄取。全程优雅降级。

semantic_search(query)  : 嵌入 query → pgvector 余弦召回 top_n → TEI rerank → top_k
build_context(query)    : 把 top 命中拼成给 AI 分析注入的证据块(检索不可用则返空串)
ingest_*                : 把各源文本嵌入入库(analysis_records / 本地新闻 / ai 推荐 / 研报)

数据源:
  analysis  ← PG analysis_records(历史多智能体分析:评级/风险/操作建议)
  news      ← 本地 db/news_flow.db platform_news(8000+ 条)
  reco      ← PG ai_recommendations(推荐理由)
  report    ← 实时抓个股研报(按需,ingest_reports(symbols))
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import List, Dict, Optional

# 路径引导:作为库被 import 时入口已 import _bootstrap;独立运行时补根目录到 sys.path
if not any(os.path.basename(p) == 'shadow-foliant' for p in sys.path):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402
import embed_client  # noqa: E402
import store  # noqa: E402


def _pg():
    import psycopg2
    return psycopg2.connect(host=os.getenv('PG_HOST'), port=int(os.getenv('PG_PORT', '5432')),
                            dbname=os.getenv('PG_DATABASE'), user=os.getenv('PG_USER'),
                            password=os.getenv('PG_PASSWORD'), connect_timeout=8)


def _batch_embed_upsert(docs: List[Dict], batch: int = 8) -> int:
    """docs: [{source_type,ref_id,title,content,meta}] → 批量嵌入 content → upsert。返回入库数。
    小批(8)+ 长超时(60s):Ollama BGE-M3 批量较慢,避免超时触发冷却级联。"""
    total = 0
    n = len(docs)
    for i in range(0, n, batch):
        chunk = docs[i:i + batch]
        embed_client._embed_down_until = 0   # 摄取时清冷却,单批失败不拖垮后续
        vecs = embed_client.embed([d['content'][:4000] for d in chunk], timeout=60)
        if not vecs:
            print(f'[rag] 嵌入失败,跳过批 {i}-{i+len(chunk)}')
            continue
        for d, v in zip(chunk, vecs):
            d['embedding'] = v
        total += store.upsert(chunk)
        if (i // batch) % 5 == 0:
            print(f'  ...嵌入入库 {total}/{n}')
    return total


# ----------------------------- 摄取 -----------------------------
def ingest_analyses(limit: int = 500) -> int:
    """PG analysis_records → 文本(名称+评级+风险+操作建议+讨论)。"""
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT id, symbol, stock_name, final_decision, discussion_result, created_at "
                    "FROM analysis_records ORDER BY id DESC LIMIT %s", (limit,))
        rows = cur.fetchall(); conn.close()
    except Exception as e:
        print(f'[rag] 读 analysis_records 失败: {type(e).__name__}'); return 0
    docs = []
    for rid, sym, name, fd, disc, ts in rows:
        fd = fd if isinstance(fd, dict) else {}
        # 文本控制在 ~600 字内(过长会拖慢/超时嵌入):名称+评级+风险+操作建议,不含整段讨论
        parts = [f"{name}({sym})", f"评级:{fd.get('rating', '')}"]
        for k in ('operation_advice', 'risk_warning', 'entry_range'):
            if fd.get(k):
                parts.append(f"{k}:{str(fd[k])[:180]}")
        docs.append({'source_type': 'analysis', 'ref_id': rid,
                     'title': f"{name} {fd.get('rating', '')}", 'content': '\n'.join(parts),
                     'meta': {'symbol': sym, 'name': name, 'rating': fd.get('rating'),
                              'date': str(ts)[:10]}})
    return _batch_embed_upsert(docs)


def ingest_news(limit: int = 2000) -> int:
    """本地 news_flow.db platform_news → 标题+内容。"""
    path = _bootstrap.db_path('news_flow.db')
    if not os.path.exists(path):
        print('[rag] 无本地 news_flow.db,跳过新闻摄取'); return 0
    try:
        c = sqlite3.connect(path)
        cols = [r[1] for r in c.execute("PRAGMA table_info(platform_news)")]
        tcol = next((x for x in ('title', 'titre', '标题') if x in cols), cols[1] if len(cols) > 1 else 'title')
        ccol = next((x for x in ('content', 'summary', 'desc', 'description') if x in cols), tcol)
        idcol = 'id' if 'id' in cols else tcol
        rows = c.execute(f"SELECT {idcol},{tcol},{ccol} FROM platform_news "
                         f"ORDER BY rowid DESC LIMIT {int(limit)}").fetchall()
        c.close()
    except Exception as e:
        print(f'[rag] 读 platform_news 失败: {type(e).__name__}: {e}'); return 0
    docs = [{'source_type': 'news', 'ref_id': f'news_{rid}', 'title': str(t or '')[:120],
             'content': (str(t or '') + ' ' + str(ct or ''))[:2000],
             'meta': {'title': str(t or '')[:120]}}
            for rid, t, ct in rows if (t or ct)]
    return _batch_embed_upsert(docs)


def ingest_recommendations() -> int:
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("SELECT id, symbol, name, rating, reason FROM ai_recommendations WHERE reason IS NOT NULL")
        rows = cur.fetchall(); conn.close()
    except Exception:
        return 0
    docs = [{'source_type': 'reco', 'ref_id': rid, 'title': f"{name} {rating}",
             'content': f"{name}({sym}) {rating}: {reason}", 'meta': {'symbol': sym, 'rating': rating}}
            for rid, sym, name, rating, reason in rows]
    return _batch_embed_upsert(docs)


def ingest_reports(symbols: List[str], per: int = 5) -> int:
    """实时抓个股研报 → 摄取(按需)。"""
    try:
        from a_stock_data_adapter import AStockDataAdapter
        ad = AStockDataAdapter()
    except Exception:
        return 0
    docs = []
    for sym in symbols:
        try:
            for r in (ad.get_reports(sym, max_pages=1) or [])[:per]:
                title = r.get('title') or r.get('报告名称') or ''
                docs.append({'source_type': 'report', 'ref_id': f"rpt_{sym}_{hash(title) & 0xffffff}",
                             'title': title[:120], 'content': (title + ' ' + str(r.get('summary', '')))[:2000],
                             'meta': {'symbol': sym}})
        except Exception:
            continue
    return _batch_embed_upsert(docs)


def ingest_dragon_tiger(days: int = 60) -> int:
    """龙虎榜归档(longhubang_records)→ 复盘文本。按(日期,个股)聚合席位/净额/概念。

    一只个股某日上榜可能多行(每席位一行),聚合成一条:个股+上榜类型+净额+主要席位+概念。
    供"龙虎榜复盘/游资动向"语义召回。无数据/无表 → 0。
    """
    try:
        from longhubang_db import get_longhubang_db
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
        df = get_longhubang_db().get_longhubang_data(start_date=start)
    except Exception as e:
        print(f'[rag] 读 longhubang_records 失败: {type(e).__name__}')
        return 0
    if df is None or len(df) == 0:
        print('[rag] 龙虎榜无数据,跳过')
        return 0
    docs = []
    for (date, code), g in df.groupby(['date', 'stock_code']):
        name = str(g['stock_name'].iloc[0]) if 'stock_name' in g else ''
        net = float(g['net_inflow'].fillna(0).sum()) if 'net_inflow' in g else 0.0
        ltypes = '/'.join(sorted({str(x) for x in g.get('list_type', []) if str(x) not in ('', 'nan', 'None')}))
        seats = [str(x) for x in (list(g.get('youzi_name', [])) + list(g.get('yingye_bu', [])))
                 if str(x) not in ('', 'nan', 'None')]
        seats = list(dict.fromkeys(seats))[:6]
        concepts = '/'.join(sorted({str(x) for x in g.get('concepts', []) if str(x) not in ('', 'nan', 'None')}))[:120]
        content = (f"{name}({code}) {date} 龙虎榜 上榜类型:{ltypes or '—'} "
                   f"净额:{round(net/10000, 2)}万 主要席位:{('、'.join(seats)) or '—'}"
                   + (f" 概念:{concepts}" if concepts else ''))
        docs.append({'source_type': 'longhubang', 'ref_id': f'lhb_{date}_{code}',
                     'title': f"{name} {date} 龙虎榜", 'content': content[:1500],
                     'meta': {'symbol': str(code), 'name': name, 'date': str(date), 'net_inflow': net}})
    return _batch_embed_upsert(docs)


def ingest_all(news_limit: int = 2000) -> Dict[str, int]:
    return {'analysis': ingest_analyses(), 'news': ingest_news(news_limit),
            'reco': ingest_recommendations(), 'longhubang': ingest_dragon_tiger()}


# ----------------------------- 检索 -----------------------------
def semantic_search(query: str, top_k: int = 8, top_n: int = 20,
                    source_types: Optional[List[str]] = None, use_rerank: bool = True) -> List[Dict]:
    """语义检索:嵌入→pgvector 召回→(可选)rerank 精排→top_k。任一环挂返回 []。"""
    qv = embed_client.embed_one(query)
    if not qv:
        return []
    cands = store.search(qv, top_n=top_n, source_types=source_types)
    if not cands:
        return []
    if use_rerank:
        rk = embed_client.rerank(query, [c['content'][:512] for c in cands])
        if rk:
            order = []
            for idx, score in rk:
                if 0 <= idx < len(cands):
                    c = dict(cands[idx]); c['score'] = round(score, 4); order.append(c)
            return order[:top_k]
    # rerank 不可用 → 用余弦距离排序兜底
    for c in cands:
        c['score'] = round(1 - c['distance'], 4)
    return cands[:top_k]


def build_context(query: str, top_k: int = 5, source_types: Optional[List[str]] = None) -> str:
    """给 AI 分析注入的证据块。检索不可用 → 返回空串(分析照常进行)。"""
    hits = semantic_search(query, top_k=top_k, source_types=source_types)
    if not hits:
        return ''
    lines = ['【相关历史/资讯检索(向量召回+精排)】']
    for i, h in enumerate(hits, 1):
        tag = {'analysis': '历史分析', 'news': '新闻', 'reco': '历史推荐', 'report': '研报',
               'longhubang': '龙虎榜'}.get(h['source_type'], h['source_type'])
        lines.append(f"{i}. [{tag}] {(h.get('title') or h['content'][:60])} — {h['content'][:160]}")
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys, io
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    from dotenv import load_dotenv
    load_dotenv()
    if '--ingest' in sys.argv:
        print('摄取:', ingest_all(news_limit=int(os.getenv('NEWS_LIMIT', '500'))))
    print('库存:', store.stats())
    for q in ['白酒龙头估值与风险', '减持 出货 风险']:
        print(f'\n查询: {q}')
        for h in semantic_search(q, top_k=3):
            print(f"  {h.get('score')}  [{h['source_type']}] {(h.get('title') or h['content'][:50])}")
