import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""LLM Token 用量遥测 —— 借鉴 daily_stock_analysis 的 LLMUsage 表设计

回答的问题:**多智能体系统每天/每个环节烧了多少 token、走了哪个 provider/model?**

全项目 LLM 调用收口在 `llm_router.call`(主)与 `deepseek_client.call_api` 直连兜底(备),
两处都在拿到响应后调用本模块 `record(...)` 落一行 `llm_usage`。本模块是**纯旁路遥测**:
  - 任何异常都被吞掉(record 永不抛),绝不阻塞/拖慢真实 LLM 调用;
  - 拿不到 usage(provider 未回 usage 字段)也记一行(tokens=0,标 ok),保留调用计数。

存储走 db_compat(SQLite/PG 双模),与 ai_recommendations 同库 jobs_snapshots.db。
聚合在 Python 端做(窗口内行数不大),规避 SQL 方言差异。

接口:
  record(call_type, used, prompt_tokens, completion_tokens, total_tokens, thinking=False, ok=True)
  record_from_resp(call_type, used, resp, thinking=False)   # 从 openai resp.usage 抽取
  summary(days=30) -> dict   # 总量 / 按 model / 按 call_type / 按天 / 最近 N 条
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from db_compat import connect as _connect, USE_POSTGRES

_DB_PATH = _bootstrap.db_path('jobs_snapshots.db')
_table_ready = False


def _ensure_table():
    global _table_ready
    if _table_ready:
        return
    try:
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        if USE_POSTGRES:
            cur.execute('''CREATE TABLE IF NOT EXISTS llm_usage (
                id SERIAL PRIMARY KEY,
                ts TEXT, call_type TEXT, provider TEXT, model TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cache_hit_tokens INTEGER DEFAULT 0,
                cache_miss_tokens INTEGER DEFAULT 0,
                thinking INTEGER DEFAULT 0, ok INTEGER DEFAULT 1)''')
        else:
            cur.execute('''CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, call_type TEXT, provider TEXT, model TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                cache_hit_tokens INTEGER DEFAULT 0,
                cache_miss_tokens INTEGER DEFAULT 0,
                thinking INTEGER DEFAULT 0, ok INTEGER DEFAULT 1)''')
        try:
            cur.execute('CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts)')
        except Exception:
            pass
        conn.commit()
        # 旧表迁移:补 cache 列(已存在则忽略)。PG 失败会脏事务 → 每条单独 rollback。
        for _col in ('cache_hit_tokens', 'cache_miss_tokens'):
            try:
                cur.execute(f'ALTER TABLE llm_usage ADD COLUMN {_col} INTEGER DEFAULT 0')
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
        conn.close()
        _table_ready = True
    except Exception as e:
        # 建表失败(无库/权限)→ 本次不可用,下次再试。绝不抛。
        print(f'[llm_usage] 建表失败(忽略遥测): {type(e).__name__}: {str(e)[:80]}')


def _split_used(used: str):
    """'deepseek:deepseek-chat' → ('deepseek', 'deepseek-chat')。无冒号则 provider=used。"""
    s = str(used or '')
    if ':' in s:
        p, m = s.split(':', 1)
        return p.strip(), m.strip()
    return s.strip(), ''


def _as_int(v) -> int:
    try:
        return int(v) if v not in (None, '') else 0
    except (TypeError, ValueError):
        return 0


def record(call_type: str, used: str,
           prompt_tokens=0, completion_tokens=0, total_tokens=0,
           thinking: bool = False, ok: bool = True,
           cache_hit_tokens=0, cache_miss_tokens=0) -> None:
    """落一行用量。永不抛异常(纯旁路遥测)。used='provider:model'。

    cache_hit_tokens/cache_miss_tokens:命中/未命中缓存的输入 token(DeepSeek 上下文缓存,
    命中按约 1/10 计价)。二者之和≈prompt_tokens;命中率 = hit/(hit+miss)。
    """
    try:
        provider, model = _split_used(used)
        if provider in ('', 'none'):   # 路由全失败的占位,不记
            return
        pt, ct = _as_int(prompt_tokens), _as_int(completion_tokens)
        tt = _as_int(total_tokens) or (pt + ct)
        ch, cm = _as_int(cache_hit_tokens), _as_int(cache_miss_tokens)
        _ensure_table()
        if not _table_ready:
            return
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''INSERT INTO llm_usage
            (ts, call_type, provider, model, prompt_tokens, completion_tokens,
             total_tokens, cache_hit_tokens, cache_miss_tokens, thinking, ok)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (datetime.now().isoformat(), str(call_type or 'misc'), provider, model,
             pt, ct, tt, ch, cm, 1 if thinking else 0, 1 if ok else 0))
        conn.commit()
        conn.close()
    except Exception:
        pass  # 遥测失败绝不影响主路径


def _usage_attr(u, key):
    """从 usage 对象(pydantic/dict 皆可)取一个字段,缺失返回 None。"""
    if u is None:
        return None
    if isinstance(u, dict):
        return u.get(key)
    v = getattr(u, key, None)
    if v is None:
        # openai SDK 把未知字段塞 model_extra(DeepSeek 的 prompt_cache_* 即在此)
        extra = getattr(u, 'model_extra', None) or getattr(u, '__pydantic_extra__', None)
        if isinstance(extra, dict):
            return extra.get(key)
    return v


def _extract_cache(u, prompt_tokens: int):
    """抽缓存命中/未命中 token(借鉴 daily_stock_analysis 的多源口径,精简为我们用到的两类):

      - DeepSeek:usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens(hit+miss≈prompt)
      - OpenAI 兼容:usage.prompt_tokens_details.cached_tokens 作命中,miss=prompt-hit

    取不到返回 (0, 0)。"""
    try:
        hit = _usage_attr(u, 'prompt_cache_hit_tokens')
        miss = _usage_attr(u, 'prompt_cache_miss_tokens')
        if hit is not None or miss is not None:
            ch, cm = _as_int(hit), _as_int(miss)
            return ch, (cm if (miss is not None) else max(prompt_tokens - ch, 0))
        details = _usage_attr(u, 'prompt_tokens_details')
        cached = _usage_attr(details, 'cached_tokens') if details is not None else None
        if cached is not None:
            ch = _as_int(cached)
            return ch, max(prompt_tokens - ch, 0)
    except Exception:
        pass
    return 0, 0


def record_from_resp(call_type: str, used: str, resp: Any, thinking: bool = False) -> None:
    """从 openai 风格响应对象抽取 usage 后落库。resp.usage 缺失则记一行 0 token。"""
    pt = ct = tt = ch = cm = 0
    try:
        u = getattr(resp, 'usage', None)
        if u is not None:
            pt = _as_int(_usage_attr(u, 'prompt_tokens'))
            ct = _as_int(_usage_attr(u, 'completion_tokens'))
            tt = _as_int(_usage_attr(u, 'total_tokens'))
            ch, cm = _extract_cache(u, pt)
    except Exception:
        pass
    record(call_type, used, pt, ct, tt, thinking=thinking, ok=True,
           cache_hit_tokens=ch, cache_miss_tokens=cm)


def summary(days: int = 30) -> Dict[str, Any]:
    """近 N 天用量汇总。返回 totals / by_model / by_call_type / by_day / recent。"""
    out: Dict[str, Any] = {
        'days': days, 'enabled': True,
        'totals': {'calls': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0,
                   'cache_hit_tokens': 0, 'cache_miss_tokens': 0, 'cache_hit_ratio': None},
        'by_model': [], 'by_call_type': [], 'by_day': [], 'recent': [],
    }
    try:
        _ensure_table()
        if not _table_ready:
            out['enabled'] = False
            return out
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn = _connect(_DB_PATH)
        cur = conn.cursor()
        cur.execute('''SELECT ts, call_type, provider, model,
                              prompt_tokens, completion_tokens, total_tokens, thinking, ok,
                              cache_hit_tokens, cache_miss_tokens
                       FROM llm_usage WHERE ts >= ? ORDER BY ts DESC''', (cutoff,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        out['enabled'] = False
        out['error'] = f'{type(e).__name__}: {str(e)[:80]}'
        return out

    def _ratio(hit: int, miss: int):
        """缓存命中率 = hit/(hit+miss);无缓存数据(分母 0)返回 None。"""
        denom = hit + miss
        return round(hit / denom, 4) if denom > 0 else None

    by_model: Dict[str, Dict[str, int]] = {}
    by_type: Dict[str, Dict[str, int]] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    t = out['totals']
    for r in rows:
        ts, ctype, provider, model = r[0], r[1] or 'misc', r[2] or '', r[3] or ''
        pt, ct, tt = _as_int(r[4]), _as_int(r[5]), _as_int(r[6])
        ch, cm = _as_int(r[9]), _as_int(r[10])
        t['calls'] += 1
        t['prompt_tokens'] += pt
        t['completion_tokens'] += ct
        t['total_tokens'] += tt
        t['cache_hit_tokens'] += ch
        t['cache_miss_tokens'] += cm
        mk = f'{provider}:{model}' if model else provider
        for d, k in ((by_model, mk), (by_type, ctype), (by_day, str(ts)[:10])):
            b = d.setdefault(k, {'calls': 0, 'total_tokens': 0,
                                 'cache_hit_tokens': 0, 'cache_miss_tokens': 0})
            b['calls'] += 1
            b['total_tokens'] += tt
            b['cache_hit_tokens'] += ch
            b['cache_miss_tokens'] += cm

    t['cache_hit_ratio'] = _ratio(t['cache_hit_tokens'], t['cache_miss_tokens'])

    def _finish(b: Dict[str, int]) -> Dict[str, Any]:
        b['cache_hit_ratio'] = _ratio(b['cache_hit_tokens'], b['cache_miss_tokens'])
        return b

    out['by_model'] = sorted(
        [{'model': k, **_finish(v)} for k, v in by_model.items()],
        key=lambda x: -x['total_tokens'])
    out['by_call_type'] = sorted(
        [{'call_type': k, **_finish(v)} for k, v in by_type.items()],
        key=lambda x: -x['total_tokens'])
    out['by_day'] = sorted(
        [{'day': k, **_finish(v)} for k, v in by_day.items()], key=lambda x: x['day'])
    out['recent'] = [
        {'ts': str(r[0]), 'call_type': r[1] or 'misc', 'provider': r[2] or '',
         'model': r[3] or '', 'total_tokens': _as_int(r[6]),
         'cache_hit_tokens': _as_int(r[9]), 'cache_miss_tokens': _as_int(r[10]),
         'thinking': bool(r[7]), 'ok': bool(r[8])}
        for r in rows[:50]]
    return out


if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print('=== llm_usage 自检 ===')
    record('selftest', 'deepseek:deepseek-chat', 100, 50, 150,
           cache_hit_tokens=80, cache_miss_tokens=20)
    record('selftest', 'deepseek:deepseek-reasoner', 200, 300, 500, thinking=True,
           cache_hit_tokens=150, cache_miss_tokens=50)

    # 模拟 DeepSeek usage 对象(extra 字段走 model_extra),验证 record_from_resp 抽缓存
    class _U:
        prompt_tokens = 300
        completion_tokens = 120
        total_tokens = 420
        model_extra = {'prompt_cache_hit_tokens': 256, 'prompt_cache_miss_tokens': 44}

    class _Resp:
        usage = _U()

    record_from_resp('selftest_resp', 'deepseek:deepseek-chat', _Resp())
    import json
    print(json.dumps(summary(days=1), ensure_ascii=False, indent=2))
