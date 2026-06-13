"""嵌入 + 精排 HTTP 客户端 —— 全程优雅降级,挂了返回 None,绝不抛进主流程。

嵌入:Ollama OpenAI 兼容接口  POST {BGE_URL} {"model":bge-m3,"input":[...]} → data[].embedding (1024 维)
精排:TEI rerank           POST {TEI_RERANK_URL}/rerank {"query","texts"} → [{index,score}]

配置(.env):
  BGE_URL=http://your_embed_server:11434/v1/embeddings
  BGE_MODEL=bge-m3
  TEI_RERANK_URL=http://your_rerank_server:8080
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import List, Optional, Tuple

DIM = 1024  # BGE-M3 dense 维度

_embed_down_until = 0.0
_rerank_down_until = 0.0
_warned = set()


def _cfg(key, default=''):
    return os.getenv(key, default)


def _post(url: str, obj: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(obj).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8'))


def embed(texts: List[str], timeout: float = 30) -> Optional[List[List[float]]]:
    """批量嵌入。成功返回 [向量...](与 texts 等长),任何失败返回 None(并冷却 30s)。"""
    global _embed_down_until
    if not texts:
        return []
    if _embed_down_until and time.time() < _embed_down_until:
        return None
    url = _cfg('BGE_URL', '')
    model = _cfg('BGE_MODEL', 'bge-m3')
    try:
        d = _post(url, {'model': model, 'input': [str(t)[:8000] for t in texts]}, timeout)
        data = d.get('data') or []
        out = [row['embedding'] for row in data]
        if len(out) != len(texts):
            return None
        return out
    except Exception as e:
        if 'embed' not in _warned:
            print(f'[rag] 嵌入服务不可用,降级(检索功能停用,不影响主功能): {type(e).__name__}')
            _warned.add('embed')
        _embed_down_until = time.time() + 30
        return None


def embed_one(text: str, timeout: float = 30) -> Optional[List[float]]:
    r = embed([text], timeout)
    return r[0] if r else None


def rerank(query: str, texts: List[str], timeout: float = 30) -> Optional[List[Tuple[int, float]]]:
    """TEI 精排。返回 [(原索引, 分数)] 按分降序;失败返回 None。"""
    global _rerank_down_until
    if not texts:
        return []
    if _rerank_down_until and time.time() < _rerank_down_until:
        return None
    url = _cfg('TEI_RERANK_URL', '').rstrip('/')
    try:
        d = _post(url + '/rerank', {'query': str(query)[:512],
                                     'texts': [str(t)[:512] for t in texts]}, timeout)
        # TEI 返回 [{'index':i,'score':s}, ...]
        return [(int(x['index']), float(x['score'])) for x in d]
    except Exception as e:
        if 'rerank' not in _warned:
            print(f'[rag] rerank 服务不可用,降级(跳过精排): {type(e).__name__}')
            _warned.add('rerank')
        _rerank_down_until = time.time() + 30
        return None


def health() -> dict:
    """探活(给设置页/诊断用)。"""
    e = embed_one('健康探测', timeout=8)
    r = rerank('a', ['b', 'c'], timeout=8)
    return {'embed_ok': e is not None, 'embed_dim': len(e) if e else None,
            'rerank_ok': r is not None}


if __name__ == '__main__':
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    from dotenv import load_dotenv
    load_dotenv()
    print('health:', health())
    v = embed_one('贵州茅台一季度净利润增长20%')
    print('embed dim:', len(v) if v else None)
    rk = rerank('茅台业绩', ['茅台净利增长20%', '平安银行不良率下降', '茅台提价预期'])
    print('rerank:', rk)
