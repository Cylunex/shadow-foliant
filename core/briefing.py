# -*- coding: utf-8 -*-
"""晨报生成 + 推送(独立模块,不依赖 webui/FastAPI)。

- build_briefing():大盘 + 多因子买入候选 + 持仓逐只信号(卖出提示/买点)。
- ai_summary():LLM 一句话操作提示(需 LLM key)。
- format_text():拼成推送文本。
- run_and_push():生成 → 总结 → 推送(钉钉/邮件,走 notification_router)。
供 webui 端点、jobs 任务、scripts/push_briefing.py 共用。
"""
from concurrent.futures import ThreadPoolExecutor

import _bootstrap  # noqa: F401


def _market():
    # 大盘+板块统一走 datahub(指数:新浪→腾讯;板块:新浪行业 akshare)
    out = {"indices": [], "sector_top": [], "sector_bottom": []}
    try:
        import datahub
        out["indices"] = [{"name": x["name"], "v": f"{x['value']:.2f} ({x['change_pct']:+.2f}%)"}
                          for x in datahub.indices()]
    except Exception:
        pass
    try:
        import datahub
        s = datahub.sector_spot(top_n=4, bottom_n=3)
        out["sector_top"] = s.get("top", [])
        out["sector_bottom"] = s.get("bottom", [])
    except Exception:
        pass
    return out


def _scan_holding(h):
    """单只持仓信号:regime + 情绪顶 + 企稳/底部放量 → 卖出分 & 买点。"""
    try:
        import datahub
        from strategy_signals import shrink_pullback, bottom_volume, emotion_top_warning, detect_regime
        code = str(h.get("code"))
        # 走 datahub.kline(磁盘缓存);6mo≈120交易日,够算 regime/信号。前复权 qfq:技术信号防除权跳空
        df = datahub.kline(code, "6mo", adjust='qfq')
        if df is None or len(df) < 30:
            return None
        regime = detect_regime(df)
        emo = emotion_top_warning(df) or {}
        bot = bottom_volume(df) or {}
        shr = shrink_pullback(df) or {}
        score, reasons = 0, []
        if regime == "trending_down":
            score += 2; reasons.append("下降趋势")
        if emo.get("signal"):
            score += 2; reasons.append("情绪顶预警:" + str(emo.get("reason", "")))
        if regime == "volatile":
            score += 1; reasons.append("高波动")
        buy_sig, buy_reason = False, ""
        if bot.get("signal"):
            buy_sig, buy_reason = True, "底部放量:" + str(bot.get("reason", ""))
        elif shr.get("signal"):
            buy_sig, buy_reason = True, "缩量回踩企稳:" + str(shr.get("reason", ""))
        return {"code": code, "name": h.get("name"), "regime": regime,
                "sell_score": score, "sell_reasons": reasons,
                "buy_signal": buy_sig, "buy_reason": buy_reason}
    except Exception:
        return None


def _cache_path():
    import os
    return os.path.join(_bootstrap.DB_DIR, "briefing_cache.json")


def cached_briefing(sell_n: int = 5, buy_n: int = 6, force: bool = False):
    """**文件缓存**晨报。语义:有缓存就展示、不点不重算。
      force=False(页面加载/默认):**只读缓存文件**——有就返(秒开),没有返 None(绝不冷算,避免卡)。
      force=True(用户点「刷新」/morning_strategy 09:00 任务):重算 build_briefing 并写缓存。
    晨报是日报,morning_strategy 每天 09:00 force 刷新一次,用户全天直接读这份缓存。
    文件缓存跨进程、抗重启,与"后台算→落盘、前端读"天然契合。"""
    import json
    import os
    p = _cache_path()
    if not force:
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and d.get("scanned") is not None:
                    d["_cached_at"] = int(os.path.getmtime(p))
                    return d
        except Exception:
            pass
        return None   # 无缓存:不冷算,由调用方提示用户手动刷新
    out = build_briefing(sell_n, buy_n)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception:
        pass
    return out


def _bounded(fn, secs, default):
    """在独立线程跑 fn,超 secs 秒放弃返回 default(被弃线程后台自然结束)。
    防 build_briefing 的外部慢步(akshare板块/多因子)在弱网/盘外无限挂住。"""
    import concurrent.futures as _cf
    try:
        return _BRIEF_POOL.submit(fn).result(timeout=secs)
    except Exception:
        return default


import concurrent.futures as _cf
_BRIEF_POOL = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="brief")


def build_briefing(sell_n: int = 5, buy_n: int = 6) -> dict:
    # _market(指数+akshare板块)、多因子选股 都可能在盘外慢源下挂住 → 各自加超时,保证整体能完成
    out = {"market": _bounded(_market, 12, {"indices": [], "sector_top": [], "sector_bottom": []}),
           "buy": [], "sell": [], "hold_buy": [], "scanned": 0}
    try:
        from multi_factor_screener import screen_index_cached
        # cache_only:晨报(09:00)也只读盘后焐好的多因子缓存,冷了就空着这段,不在早盘现拉 300 只
        r = _bounded(lambda: screen_index_cached(index_code="000300", n=buy_n, add_sector_leaders=True,
                                                 workers=1, cache_only=True),
                     25, {}) or {}
        buy = [{"code": x.get("symbol"), "composite": round(float(x.get("composite") or 0), 3)}
               for x in (r.get("top") or [])[:buy_n]]
        try:
            import datahub
            q = datahub.quotes([b["code"] for b in buy]) or {}
            for b in buy:
                info = q.get(b["code"]) or q.get(str(b["code"])[-6:]) or {}  # datahub.quotes 归一key:带前缀也兜底
                b["name"], b["price"] = info.get("name"), info.get("price")
        except Exception:
            pass
        out["buy"] = buy
    except Exception:
        pass
    try:
        from concurrent.futures import as_completed
        from portfolio_db import portfolio_db
        holds = portfolio_db.get_all_stocks() or []
        scans = []
        # 总时限 50s:盘外个别源拉不动时不无限等,取已完成的(注明部分);K线落盘后下次就快
        ex = ThreadPoolExecutor(max_workers=12)
        futs = {ex.submit(_scan_holding, h): h for h in holds}
        try:
            for fut in as_completed(futs, timeout=50):
                try:
                    s = fut.result()
                    if s:
                        scans.append(s)
                except Exception:
                    pass
        except Exception:
            pass  # 整体超时:用已完成的
        ex.shutdown(wait=False)
        out["scanned"] = len(scans)
        out["partial"] = len(scans) < len(holds)
        out["sell"] = sorted([s for s in scans if s["sell_score"] > 0],
                             key=lambda x: x["sell_score"], reverse=True)[:sell_n]
        out["hold_buy"] = [s for s in scans if s["buy_signal"]][:8]
    except Exception as e:
        out["sell_error"] = str(e)[:100]
    return out


def ai_summary(b: dict) -> str:
    """LLM 一句话操作提示。失败返回空串。"""
    try:
        from llm_router import get_router
        m = b.get("market", {})
        buy = ",".join(x.get("code", "") for x in b.get("buy", []))
        sell = ",".join("%s(%s)" % (x["name"], "/".join(x["sell_reasons"])) for x in b.get("sell", []))
        user = (f"大盘:{m.get('indices')}\n强势板块:{m.get('sector_top')}\n"
                f"多因子买入候选:{buy}\n持仓建议关注卖出:{sell}\n"
                "用3-4句话给今日操作要点:大盘基调、是否加减仓、重点提示。简洁口语。")
        text, _ = get_router().call(
            [{"role": "system", "content": "你是私人投资助理,给懒人用户简洁的每日操作提示。"},
             {"role": "user", "content": user}], temperature=0.5, max_tokens=500)
        return (text or "").strip()
    except Exception:
        return ""


def format_text(b: dict, ai: str = "") -> str:
    L = ["☀️ 今日晨报", ""]
    if ai:
        L += ["【操作提示】", ai.strip(), ""]
    m = b.get("market") or {}
    if m.get("indices"):
        L.append("【大盘】" + "  ".join(f"{x['name']}{x['v']}" for x in m["indices"]))
    if m.get("sector_top"):
        L.append("强势板块: " + "、".join(f"{s['板块']}{s['涨跌幅']}%" for s in m["sector_top"]))
    L.append("")
    if b.get("buy"):
        L.append("【买入候选(多因子)】")
        L += [f"· {x.get('name') or x['code']} {x['code']} 综合分{x.get('composite')}" for x in b["buy"]]
        L.append("")
    if b.get("sell"):
        L.append("【持仓建议关注卖出】")
        L += [f"· {s['name']} {s['code']} 风险分{s['sell_score']}: {'/'.join(s['sell_reasons'])}" for s in b["sell"]]
        L.append("")
    if b.get("hold_buy"):
        L.append("【持仓出现买点】")
        L += [f"· {s['name']} {s['code']}: {s['buy_reason']}" for s in b["hold_buy"]]
    return "\n".join(L)


def run_and_push(with_ai: bool = True) -> dict:
    """生成晨报 → (可选)AI总结 → 推送。返回 {sent, channels, text_len}。"""
    b = build_briefing()
    ai = ai_summary(b) if with_ai else ""
    text = format_text(b, ai)
    sent = {}
    try:
        from notification_router import send
        sent = send("report", "☀️ 今日晨报", text)
    except Exception as e:
        sent = {"error": str(e)}
    return {"sent": sent, "buy": len(b.get("buy", [])), "sell": len(b.get("sell", [])),
            "scanned": b.get("scanned"), "text_len": len(text)}


if __name__ == "__main__":
    import sys, io, json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    print(json.dumps(run_and_push(), ensure_ascii=False))
