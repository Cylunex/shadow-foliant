"""WebUI 原型后端 —— FastAPI,复用项目现有纯函数,暴露 REST 给 SPA 前端。

定位:替代 Streamlit 的"新 UI 方案"原型(用户选 FastAPI + React/Vue)。本原型前端用 Vue3 免构建版
(webui/static/index.html),后端这层是真正的 REST,生产换 Vite+React/Vue 时后端不变。

运行:
    cd <项目根>
    uvicorn webui.api_server:app --port 8600 --reload
    # 浏览器开 http://localhost:8600
本机 .env 是内网 PG(可能不通),试用建议:  USE_POSTGRES=false uvicorn webui.api_server:app --port 8600
"""

from __future__ import annotations

import os
import sys

# 路径引导(webui/ 子目录)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

app = FastAPI(title="shadow-foliant WebUI 原型", version="0.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.middleware("http")
async def _no_store_static(request, call_next):
    """静态资源不缓存(开发期改了 css/js 立即生效,免浏览器缓存旧版)。"""
    resp = await call_next(request)
    if not request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


_MEM_CACHE: dict = {}  # 进程内存兜底:key -> (expire_ts, value)。Redis 未装/不可用时避免重复抓取风暴。


def _cache_or(key, ttl, compute, keep=lambda v: v is not None):
    """短TTL 缓存包装:命中即返,否则现算;仅当 keep(结果) 为真才写缓存。
    两级:① 进程内存(L1,始终生效,跨刷新去重)② Redis(L2,跨进程共享,未装则自动跳过)。
    生产 Redis 常未装(venv 缺 redis 包)→ 旧实现等价直算,盘外重复加载每次都打外部接口卡 10s+;
    现 L1 内存兜底让同键 ttl 内秒回。用于报价/压力测试/指数等重复抓取。"""
    import time
    now = time.time()
    ent = _MEM_CACHE.get(key)
    if ent and ent[0] > now:
        return ent[1]
    # L2 Redis 命中
    try:
        from cache import cache_get
        hit = cache_get(key)
        if hit is not None:
            _MEM_CACHE[key] = (now + ttl, hit)
            return hit
    except Exception:
        pass
    val = compute()
    if keep(val):
        _MEM_CACHE[key] = (now + ttl, val)
        try:
            from cache import cache_set
            cache_set(key, val, ttl)
        except Exception:
            pass
    return val


import concurrent.futures as _cf
_DEADLINE_POOL = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="deadline")


def _with_deadline(fn, seconds, default):
    """在共享线程池跑 fn,超 seconds 秒就放弃返回 default(被弃线程后台自然跑完,结果丢弃)。
    用于外部行情/净值抓取:弱网/盘外接口可能阻塞 10s+,页面不该被拖死。
    注意:不能用 `with ThreadPoolExecutor()`——其 __exit__ 会 shutdown(wait=True) 阻塞等线程跑完,
    使超时护栏形同虚设;故用模块级常驻池 + 仅对 future 设超时。"""
    fut = _DEADLINE_POOL.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except Exception:
        return default


def _jsonsafe(o):
    """递归把 NaN/Inf → None + numpy 标量 → python 原生,保证 JSON 可序列化(否则 Starlette 抛 500)。
    numpy.bool_/integer/floating 不是 JSON 原生类型(如策略/形态用 numpy 比较产出 np.bool_)→ 必须降级。"""
    import math
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _jsonsafe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonsafe(v) for v in o]
    # numpy 标量降级(不硬依赖 numpy:没装就跳过)
    if hasattr(o, "item") and type(o).__module__ == "numpy":
        try:
            return _jsonsafe(o.item())
        except Exception:
            return None
    return o


def _f(v):
    """宽松转 float;空/NaN/非数 → None。"""
    try:
        import math
        if v is None or v == "":
            return None
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except Exception:
        return None


def _try(*sources):
    """依次尝试多个取数源(无参 thunk),返回第一个"非空"结果;全失败/全空 → None。
    用于外部数据源切换兜底:主源挂了自动换备源,全挂才失败。"""
    for fn in sources:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            continue
    return None


def _llm_analyze(system: str, user: str, max_tokens: int = 1200, temperature: float = 0.5):
    """统一 LLM 调用(复用 llm_router)。返回 {analysis, provider} 或 {error}。⚠️ 需 LLM key。"""
    try:
        from llm_router import get_router
        text, provider = get_router().call(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens)
        return {"analysis": (text or "").strip(), "provider": provider}
    except Exception as e:
        return {"error": f"LLM 调用失败: {type(e).__name__}: {e}"}


def _ok(data):
    return {"ok": True, "data": _jsonsafe(data)}


def _err(msg):
    return {"ok": False, "error": str(msg)}


def _records(x):
    """DataFrame → records(NaN/Inf → None,JSON 安全);其余原样返回。
    注:df.where(..,None) 在 float 列会把 None 退回成 NaN,故在 dict 层逐值清洗。"""
    try:
        import pandas as pd
        import math
        if isinstance(x, pd.DataFrame):
            recs = x.to_dict("records")
            for r in recs:
                for k, v in r.items():
                    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                        r[k] = None
            return recs
    except Exception:
        pass
    return x


# ============================ 股票(首页) ============================
@app.get("/api/stock/{code}")
def stock_info(code: str):
    try:
        import datahub
        q = _cache_or(f"quote:{code}", 30, lambda: datahub.quote(code),
                      keep=lambda v: isinstance(v, dict) and v.get("price"))
        return _ok({"code": code, **q})
    except Exception as e:
        return _err(e)


@app.get("/api/stock/{code}/kline")
def stock_kline(code: str, period: str = "1y"):
    def compute():
        import datahub
        df = datahub.kline(code, period)   # 走 datahub:复用磁盘缓存(与回测/因子/预热共享,盘后预热后 0ms)
        if df is None or getattr(df, "empty", True):
            return None
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        close_col = "Close" if "Close" in df.columns else "close"
        return [{"date": str(d)[:10], "close": float(c)}
                for d, c in zip(df[date_col], df[close_col]) if c == c]
    try:
        # 日K 盘中变动有限,缓存 10 分钟(非空才缓存)
        out = _cache_or(f"kline:{code}:{period}", 600, compute, keep=lambda v: bool(v))
        if not out:
            return _err("无数据")
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/stock/{code}/insights")
def stock_insights(code: str):
    """个股研究聚合:缠论 / 筹码分布 / 策略信号 / 财务排雷 / 资金流。
    各自 try 隔离(一项失败不影响其他),并发计算,Redis 缓存 10min。"""
    def compute():
        import datahub
        from concurrent.futures import ThreadPoolExecutor
        df = datahub.kline(code, "1y", adjust='qfq')   # 缠论/筹码/策略信号:前复权,复用磁盘缓存
        out = {}
        if df is None or getattr(df, "empty", True):
            return {"error": "无行情数据"}

        def chan():
            from chan_theory import analyze_chan
            return analyze_chan(df, code)

        def chip():
            from chip_distribution import chip_distribution as _cyq
            return _cyq(df)

        def signals():
            from strategy_signals import shrink_pullback, bottom_volume, emotion_top_warning, detect_regime
            return {"regime": detect_regime(df), "shrink_pullback": shrink_pullback(df),
                    "bottom_volume": bottom_volume(df), "emotion_top_warning": emotion_top_warning(df)}

        def forensics():
            from fundamental_scoring import collect_factors
            from financial_forensics import analyze_forensics
            return analyze_forensics(collect_factors(code) or {})

        def flow():
            import datahub
            return datahub.capital_flow_adata(code)

        tasks = {"chan": chan, "chip": chip, "signals": signals, "forensics": forensics, "flow": flow}
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {k: ex.submit(fn) for k, fn in tasks.items()}
            for k, fu in futs.items():
                try:
                    out[k] = fu.result(timeout=25)
                except Exception as e:
                    out[k] = {"error": str(e)[:120]}
        return out
    try:
        return _ok(_cache_or(f"insights:{code}", 600, compute, keep=lambda v: isinstance(v, dict) and "error" not in v))
    except Exception as e:
        return _err(e)


@app.get("/api/stock/{code}/dcf")
def stock_dcf(code: str, growth: float = 0.10, years: int = 5,
              terminal: float = 0.03, discount: float = 0.10):
    """两阶段 DCF 内在价值估值(补 PE/PB 之外的绝对估值)。
    自 stock_info 推导:股本=市值/现价、基准FCF≈净利润=市值/PE(净利近似,注明)。
    可调 高速增速 growth/高速年数 years/永续增速 terminal/折现率 discount。返回内在价值/安全边际/敏感性表。"""
    try:
        import datahub
        import dcf_valuation
        info = datahub.stock_info(code) or {}
        mc = info.get("market_cap")
        px = info.get("current_price")
        pe = info.get("pe_ratio")
        if not (mc and px and px > 0):
            return _err("无法获取市值/现价")
        if not (pe and pe > 0):
            return _err("该股 PE 不可用(亏损或缺数据),无法用净利润近似 FCF")
        shares = mc / px
        base_fcf = mc / pe   # 净利润近似(trailing)
        r = dcf_valuation.analyze_dcf(
            base_fcf=base_fcf, shares=shares, current_price=px,
            high_growth=growth, high_years=int(years), terminal_growth=terminal,
            discount_rate=discount, fcf_is_proxy=True)
        if "error" in r:
            return _err(r["error"])
        sens = dcf_valuation.sensitivity(
            base_fcf, growth, int(years), shares, 0.0, px,
            wacc_range=[round(discount - 0.02, 4), discount, round(discount + 0.02, 4)],
            tg_range=[max(0.005, round(terminal - 0.01, 4)), terminal, round(terminal + 0.01, 4)])
        r["sensitivity"] = sens["sensitivity"]
        r["code"], r["name"] = code, info.get("name")
        return _ok(r)
    except Exception as e:
        return _err(e)


@app.get("/api/stock/{code}/backtest")
def stock_backtest(code: str, strategy: str = "enter", hold_days: int = 10,
                   stop_pct: float = 8.0, target_pct: float = 15.0, lookback_days: int = 365):
    """单股策略回测(双收益):裸持有 vs 带止损止盈纪律 + 触发率。"""
    try:
        from stock_data import StockDataFetcher
        from backtest_engine import backtest_one
        from datetime import datetime, timedelta
        df = StockDataFetcher().get_stock_data(code, "2y")
        if isinstance(df, dict):
            return _err("无行情数据")
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        r = backtest_one(code, strategy, df, start, end, hold_days=hold_days,
                         stop_pct=stop_pct, target_pct=target_pct)
        return _ok({"symbol": code, "strategy": strategy, "period": r.get("period"), "summary": r.get("summary", {})})
    except Exception as e:
        return _err(e)


class PortfolioBacktestReq(BaseModel):
    codes: list[str] = []          # 自定义股票池(空则按 universe 取)
    universe: str = "holdings"     # holdings(我的持仓) | index(指数成分) | custom(用 codes)
    index_code: str = "000300"     # universe=index 时的指数
    limit: int = 50                # universe 取数上限(逐股拉K线,防慢)
    start: str = ""                # 空=近2年
    end: str = ""                  # 空=今天
    strategy: str = "enter"
    use_live: bool = False         # True=用策略基因组 live 集(各策略最优变体+组合策略)
    hold_days: int = 10
    stop_pct: float = 8.0
    target_pct: float = 15.0
    max_positions: int = 5
    initial_cash: float = 1_000_000.0
    allocation: str = "equal"      # equal 等权 | signal 信号强度加权
    benchmark: str = "000300"
    attribution: bool = False      # True=附分层归因(交易/β回归/市况/蒙特卡洛)


@app.post("/api/backtest/portfolio")
def backtest_portfolio(req: PortfolioBacktestReq):
    """组合级回测:一个现金账户、并发持仓上限、先卖后买、含交易成本,
    输出组合 CAGR/最大回撤/夏普/胜率/净值曲线并对比沪深300。无前视(次日开盘建仓)。
    单股回测(/api/stock/.../backtest)系统性高估收益,组合回测才是实盘口径。"""
    try:
        from datetime import datetime, timedelta
        from portfolio_backtest import portfolio_backtest, portfolio_backtest_live

        # —— 解析股票池 → [(code, name)] ——
        stocks = []
        if req.universe == "custom" or req.codes:
            stocks = [(c, "") for c in req.codes if c]
        elif req.universe == "holdings":
            from portfolio_db import portfolio_db
            stocks = [(str(s.get("code")), s.get("name") or "")
                      for s in (portfolio_db.get_all_stocks() or [])
                      if float(s.get("quantity") or s.get("shares") or 0) > 0 and s.get("code")]
        elif req.universe == "index":
            from multi_factor_screener import get_index_universe
            stocks = [(c, "") for c in get_index_universe(req.index_code)]
        if not stocks:
            return _err("股票池为空(无持仓/无成分/未填代码)")
        stocks = stocks[:max(1, min(req.limit, 200))]

        end = req.end or datetime.now().strftime("%Y-%m-%d")
        start = req.start or (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

        common = dict(
            hold_days=req.hold_days, stop_pct=req.stop_pct, target_pct=req.target_pct,
            max_positions=req.max_positions, initial_cash=req.initial_cash,
            allocation=req.allocation, benchmark=req.benchmark or None,
            curve_points=150,
        )
        if req.use_live:
            r = portfolio_backtest_live(stocks, start, end, **common)
        else:
            r = portfolio_backtest(stocks, start, end, strategy_id=req.strategy, **common)
        if "error" in r:
            return _err(r.get("error"))
        # 分层归因须在裁剪 trades 前算(用全量成交)
        if req.attribution:
            try:
                from backtest_attribution import attribute
                r["attribution"] = attribute(r)
            except Exception as ae:
                r["attribution"] = {"ok": False, "error": str(ae)[:120]}
        # trades 可能很多,只回最近 60 笔(摘要+曲线为主)
        r["trades"] = r.get("trades", [])[-60:]
        return _ok(r)
    except Exception as e:
        return _err(e)


@app.post("/api/stock/{code}/deep-analysis")
def stock_deep_analysis(code: str):
    """⚠️ 多智能体深度分析(技术+基本面+风险→讨论→决策)。3 agent 并行,共3轮LLM调用。
    
    同股票同天限1次:再次分析直接返回缓存结果。
    """
    import datetime
    today = datetime.date.today().isoformat()
    
    # 1. 检查今日缓存（复用 analysis_records 表）
    try:
        from database_pg import get_conn
        import psycopg2.extras
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, agents_results, discussion_result, final_decision, 
                   created_at, analysis_date
            FROM analysis_records
            WHERE symbol = %s AND DATE(analysis_date) = %s
            ORDER BY created_at DESC LIMIT 1
        """, (code, today))
        cached = cur.fetchone()
        cur.close()
        conn.close()
        if cached:
            return _ok({
                "decision": cached["final_decision"],
                "discussion": str(cached.get("discussion_result", ""))[:2500],
                "cached": True,
                "cached_at": cached["created_at"].isoformat() if cached.get("created_at") else "",
            })
    except Exception:
        pass  # DB 不可用则直算
    
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from stock_data import StockDataFetcher
        from ai_agents import StockAnalysisAgents
        A = StockAnalysisAgents()
        fetch = StockDataFetcher()
        info = fetch.get_stock_info(code)
        df = fetch.get_stock_data(code)
        if isinstance(df, dict):
            return _err("无行情数据")
        ind = fetch.get_latest_indicators(fetch.calculate_technical_indicators(df))
        fin = fetch.get_financial_data(code)

        # 3 agent 并行调用（技术/基本面/风险同时跑，减少等待时间）
        res = {}
        def _run_tech():
            return 'technical', A.technical_analyst_agent(info, df, ind)
        def _run_fund():
            return 'fundamental', A.fundamental_analyst_agent(info, fin, None)
        def _run_risk():
            return 'risk_management', A.risk_management_agent(info, ind, None, stock_data=df)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(f): f.__name__ for f in [_run_tech, _run_fund, _run_risk]}
            for future in as_completed(futures):
                key, val = future.result()
                res[key] = val
        disc = A.conduct_team_discussion(res, info)
        dec = A.make_final_decision(disc, info, ind)
        # RAG 证据增强(向量召回相关历史分析/新闻/研报;服务挂了返回空,不影响分析)
        evidence = ""
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rag"))
            import service as _rag
            evidence = _rag.build_context(f"{info.get('name','')} {code} 业绩 风险 估值", top_k=5)
        except Exception:
            pass
        result = {"decision": dec, "discussion": disc[:2500], "rag_evidence": evidence}
        # 2. 存档到 analysis_records（复用现有表）
        try:
            from database_pg import StockAnalysisDatabasePG
            db = StockAnalysisDatabasePG()
            db.save_analysis(
                symbol=code,
                stock_name=info.get('name', ''),
                period="deep",
                stock_info=info,
                agents_results=res,
                discussion_result={"summary": disc[:2500]},
                final_decision=dec,
            )
        except Exception:
            pass
        return _ok(result)
    except Exception as e:
        return _err(e)


# ============================ 分析历史 ============================

def _parse_history_rows(rows):
    """共享的历史记录行解析"""
    import json
    history = []
    for r in rows:
        fd = r.get("final_decision", {})
        if isinstance(fd, str):
            try: fd = json.loads(fd)
            except: pass
        summary = ""
        target = ""
        stop = ""
        if isinstance(fd, dict):
            summary = str(fd.get("operation_advice", "") or fd.get("risk_warning", ""))[:200]
            target = str(fd.get("target_price", ""))
            stop = str(fd.get("stop_loss", ""))
        history.append({
            "id": r["id"],
            "date": str(r.get("date", "")),
            "symbol": r.get("symbol", ""),
            "stock_name": r.get("stock_name", ""),
            "rating": fd.get("rating", "") if isinstance(fd, dict) else "",
            "target_price": target,
            "stop_loss": stop,
            "summary": summary,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
        })
    return history


@app.get("/api/stock/{code}/deep-analysis/history")
def stock_deep_analysis_history(code: str, limit: int = 10,
                                 date_from: str = None, date_to: str = None,
                                 rating: str = None):
    """查看某股票的历史深度分析记录。可选筛选：date_from/date_to(YYYY-MM-DD), rating(买入/持有/卖出)"""
    try:
        from database_pg import get_conn
        import psycopg2.extras
        import json
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where = ["symbol = %s"]
        params = [code]
        if date_from:
            where.append("DATE(analysis_date) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(analysis_date) <= %s")
            params.append(date_to)
        if rating:
            where.append("final_decision->>'rating' = %s")
            params.append(rating)
        where_sql = " AND ".join(where)
        cur.execute(f"""
            SELECT id, symbol, stock_name, DATE(analysis_date)::text as date,
                   final_decision, created_at
            FROM analysis_records
            WHERE {where_sql}
            ORDER BY created_at DESC LIMIT %s
        """, params + [limit])
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = _parse_history_rows(rows)
        return _ok(history)
    except Exception as e:
        return _err(e)


@app.get("/api/deep-analysis/history/all")
def deep_analysis_history_all(limit: int = 50,
                               date_from: str = None, date_to: str = None,
                               rating: str = None):
    """查看所有股票的深度分析历史记录。可选筛选：date_from/date_to(YYYY-MM-DD), rating(买入/持有/卖出)"""
    try:
        from database_pg import get_conn
        import psycopg2.extras
        import json
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where = ["1=1"]
        params = []
        if date_from:
            where.append("DATE(analysis_date) >= %s")
            params.append(date_from)
        if date_to:
            where.append("DATE(analysis_date) <= %s")
            params.append(date_to)
        if rating:
            where.append("final_decision->>'rating' = %s")
            params.append(rating)
        where_sql = " AND ".join(where)
        cur.execute(f"""
            SELECT id, symbol, stock_name, DATE(analysis_date)::text as date,
                   final_decision, created_at
            FROM analysis_records
            WHERE {where_sql}
            ORDER BY created_at DESC LIMIT %s
        """, params + [limit])
        rows = cur.fetchall()
        cur.close()
        conn.close()
        history = _parse_history_rows(rows)
        return _ok(history)
    except Exception as e:
        return _err(e)


# ============================ 监测 ============================
@app.get("/api/monitor/stocks")
def monitor_stocks():
    try:
        from monitor_db import monitor_db
        return _ok(monitor_db.get_monitored_stocks())
    except Exception as e:
        return _err(e)


@app.get("/api/monitor/notifications")
def monitor_notifications(limit: int = 20):
    try:
        from monitor_db import monitor_db
        return _ok(monitor_db.get_all_recent_notifications(limit))
    except Exception as e:
        return _err(e)


# ============================ 基金 ============================
# ⚠️ 路由顺序:静态路径(holdings/transactions/plans)必须在 `/api/fund/{code}` 之前定义,
#    否则 `{code}` 会把 "holdings" 当成基金代码捕获(FastAPI 按定义顺序匹配)。
#    故 fund_info(/api/fund/{code})放到本组最后,见文件下方。
@app.get("/api/fund/{code}/nav")
def fund_nav(code: str):
    try:
        import fund_data
        df = fund_data.get_nav_history(code)
        if df is None or df.empty:
            return _err("净值获取失败")
        out = [{"date": d.strftime("%Y-%m-%d"), "nav": float(n)}
               for d, n in zip(df["date"], df["unit_nav"]) if n == n]
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/fund/{code}/score")
def fund_score(code: str, extras: bool = False):
    try:
        import fund_analysis
        return _ok(fund_analysis.score_fund(code, with_extras=extras))
    except Exception as e:
        return _err(e)


class DcaReq(BaseModel):
    code: str
    amount: float = 1000
    period: str = "monthly"
    day: int = 5
    strategy: str = "normal"


@app.post("/api/fund/dca-backtest")
def fund_dca(req: DcaReq):
    try:
        import fund_data, fund_dca
        df = fund_data.get_nav_history(req.code)
        if df is None or df.empty:
            return _err("净值获取失败")
        r = fund_dca.dca_backtest(df, req.amount, req.period, day=req.day, strategy=req.strategy)
        r.pop("trades", None)  # 列表大,前端不需要逐期
        return _ok(r)
    except Exception as e:
        return _err(e)


class DcaCompareReq(BaseModel):
    code: str
    amount: float = 1000
    period: str = "monthly"
    day: int = 5


@app.post("/api/fund/dca-compare")
def fund_dca_compare(req: DcaCompareReq):
    """一键对比三种定投策略(normal 定额 / valuation 估值智能 / value_avg 价值平均)在同一基金、
    同一周期下的收益/年化IRR/最大回撤/是否跑赢一次性买入。净值只抓一次,三策略复用。"""
    try:
        import fund_data, fund_dca
        df = fund_data.get_nav_history(req.code)
        if df is None or df.empty:
            return _err("净值获取失败")
        keep = ("total_invested", "final_value", "profit_pct", "annualized_irr",
                "max_drawdown", "dca_beats_lump", "n_invests", "lump_sum", "start", "end")
        out = []
        for strat in ("normal", "valuation", "value_avg"):
            try:
                r = fund_dca.dca_backtest(df, req.amount, req.period, day=req.day, strategy=strat)
                if r.get("error"):
                    out.append({"strategy": strat, "error": r["error"]})
                else:
                    out.append({"strategy": strat, **{k: r.get(k) for k in keep}})
            except Exception as e:
                out.append({"strategy": strat, "error": str(e)[:80]})
        return _ok({"code": req.code, "period": req.period, "amount": req.amount, "results": out})
    except Exception as e:
        return _err(e)


@app.get("/api/fund/holdings")
def fund_holdings(realtime: bool = False):
    """我的基金持有。净值**优先读库**(fund_nav 表)→ 秒开;库里缺的才现抓一次并落库。
    realtime=1 时额外抓盘中估值(慢,盘后无意义,默认关)。"""
    try:
        import fund_db, fund_data
        from concurrent.futures import ThreadPoolExecutor
        fund_db.init_db()
        holdings = fund_db.get_holdings()
        if not holdings:
            return _ok([])
        codes = [str(h.get("code")) for h in holdings]
        db_navs = fund_db.get_latest_navs(codes)            # 一次查库拿全部最新净值

        name_of = {str(h.get("code")): (h.get("name") or "") for h in holdings}

        def fetch_nav(code):
            """库里没有该基金净值 → 现抓一次并落库(下次直接读库)。
            货币基金净值恒为1 且 latest_nav 抓不到(会拖慢) → 直接按1落库。"""
            if "货币" in name_of.get(code, ""):
                try:
                    fund_db.upsert_nav(code, None, 1.0)
                except Exception:
                    pass
                return {"unit_nav": 1.0, "nav_date": None}
            row = fund_data.latest_nav(code)
            if row and row.get("unit_nav"):
                try:
                    fund_db.upsert_nav(code, row.get("date"), row["unit_nav"],
                                       row.get("acc_nav"), row.get("daily_return"))
                except Exception:
                    pass
                return {"unit_nav": float(row["unit_nav"]), "nav_date": row.get("date")}
            return None

        missing = [c for c in codes if c not in db_navs]
        if missing:
            with ThreadPoolExecutor(max_workers=min(12, len(missing))) as ex:
                for c, r in zip(missing, ex.map(fetch_nav, missing)):
                    if r:
                        db_navs[c] = r

        def est_of(code, nav):
            if not realtime:
                return nav
            rt = _cache_or(f"fundgsz:{code}", 120, lambda: fund_data.get_realtime_estimate(code),
                           keep=lambda v: isinstance(v, dict) and v.get("gsz"))
            return float((rt or {}).get("gsz") or 0) or nav

        out = []
        for h in holdings:
            code = str(h.get("code"))
            shares = float(h.get("shares") or 0)
            cost_nav = float(h.get("cost_nav") or 0)
            info = db_navs.get(code) or {}
            nav = float(info.get("unit_nav") or cost_nav or 0)
            daily_return = float(info.get("daily_return") or 0) if info.get("daily_return") is not None else None
            est = est_of(code, nav)
            mv = round(shares * est, 2)
            cost = shares * cost_nav
            out.append({
                "code": code, "name": h.get("name"), "shares": round(shares, 2),
                "cost_nav": cost_nav, "nav": nav, "est_nav": round(est, 4),
                "nav_date": info.get("nav_date"),
                "mv": mv, "cost": round(cost, 2),
                "pnl": round(mv - cost, 2),
                "pnl_pct": round((est - cost_nav) / cost_nav, 4) if cost_nav else None,
                "daily_return": daily_return, "today_pnl": round(mv * daily_return / 100, 2) if daily_return else None,
            })
        out.sort(key=lambda x: x["mv"], reverse=True)
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.post("/api/fund/nav-refresh")
def fund_nav_refresh():
    """刷新所有持有基金的最新净值并落库(net值每日收盘更新一次,手动/每日跑一次即可)。"""
    try:
        import fund_db, fund_data
        from concurrent.futures import ThreadPoolExecutor
        fund_db.init_db()
        codes = [str(h.get("code")) for h in fund_db.get_holdings()]
        if not codes:
            return _ok({"updated": 0})

        def one(code):
            row = fund_data.latest_nav(code)
            if row and row.get("unit_nav"):
                try:
                    fund_db.upsert_nav(code, row.get("date"), row["unit_nav"],
                                       row.get("acc_nav"), row.get("daily_return"))
                    return 1
                except Exception:
                    return 0
            return 0

        with ThreadPoolExecutor(max_workers=12) as ex:
            n = sum(ex.map(one, codes))
        return _ok({"updated": n, "total": len(codes)})
    except Exception as e:
        return _err(e)


class FundTxnReq(BaseModel):
    code: str
    txn_type: str = "申购"          # 申购/定投/赎回
    nav: float
    amount: float | None = None     # 申购给金额
    shares: float | None = None     # 赎回给份额
    fee: float = 0.0
    trade_date: str | None = None
    name: str | None = None
    note: str | None = None


@app.post("/api/fund/transaction")
def fund_add_transaction(req: FundTxnReq):
    """记一笔申赎/定投(自动按移动加权成本更新持有)。"""
    try:
        import fund_db
        fund_db.init_db()
        r = fund_db.add_transaction(
            code=req.code, txn_type=req.txn_type, nav=req.nav, amount=req.amount,
            shares=req.shares, fee=req.fee, trade_date=req.trade_date, name=req.name, note=req.note)
        return _ok(r)
    except Exception as e:
        return _err(e)


@app.get("/api/fund/transactions")
def fund_transactions(code: str = ""):
    try:
        import fund_db
        fund_db.init_db()
        return _ok(fund_db.get_transactions(code or None))
    except Exception as e:
        return _err(e)


@app.delete("/api/fund/holdings/{code}")
def fund_delete_holding(code: str):
    """从持有列表移除某基金(不删历史流水)。"""
    try:
        import fund_db
        fund_db.delete_holding(code)
        return _ok({"deleted": code})
    except Exception as e:
        return _err(e)


@app.get("/api/fund/plans")
def fund_plans():
    try:
        import fund_db
        fund_db.init_db()
        return _ok(fund_db.get_plans())
    except Exception as e:
        return _err(e)


class FundPlanReq(BaseModel):
    code: str
    amount: float
    period: str = "monthly"
    day_of: int = 1
    strategy: str = "normal"
    name: str | None = None
    target_profit_pct: float | None = None
    auto_record: bool = False


@app.post("/api/fund/plan")
def fund_add_plan(req: FundPlanReq):
    try:
        import fund_db
        fund_db.init_db()
        pid = fund_db.add_plan(code=req.code, amount=req.amount, period=req.period,
                               day_of=req.day_of, strategy=req.strategy, name=req.name,
                               target_profit_pct=req.target_profit_pct, auto_record=req.auto_record)
        return _ok({"id": pid})
    except Exception as e:
        return _err(e)


class FundTxnsImportReq(BaseModel):
    rows: list
    update_position: bool = True
    skip_existing: bool = False


@app.post("/api/fund/transactions/import")
def fund_import_transactions(req: FundTxnsImportReq):
    """批量导入基金申赎/定投流水(按日期升序应用,移动加权成本)。⚠️ 增量叠加,勿重复导入同批。
    rows: [{code,txn_type,nav,amount|shares,fee?,trade_date?,name?,note?}]。返回 {imported,skipped,errors}。"""
    try:
        import fund_db
        return _ok(fund_db.import_transactions(req.rows, update_position=req.update_position,
                                               skip_existing=req.skip_existing))
    except Exception as e:
        return _err(e)


class FundPlansImportReq(BaseModel):
    rows: list
    dedup: bool = True


@app.post("/api/fund/plans/import")
def fund_import_plans(req: FundPlansImportReq):
    """批量导入定投计划。rows: [{code,amount,period?,day_of?,strategy?,name?,target_profit_pct?,auto_record?,note?}]。
    dedup=True 跳过已存在同 code。返回 {imported,skipped,errors}。"""
    try:
        import fund_db
        return _ok(fund_db.import_plans(req.rows, dedup=req.dedup))
    except Exception as e:
        return _err(e)


@app.get("/api/fund/screen")
def fund_screen(type: str = "股票型", sort_by: str = "r_1y", top_n: int = 20,
                min_1y: float | None = None, min_3y: float | None = None,
                max_fee: float | None = None):
    """基金同类排行筛选。type: 全部/股票型/混合型/债券型/指数型/QDII/LOF/FOF;
    sort_by: r_1w/r_1m/r_3m/r_6m/r_1y/r_2y/r_3y/r_ytd/r_since;
    可选过滤:min_1y/min_3y(近1/3年收益%下限)、max_fee(手续费%上限)。Redis 缓存 1h。"""
    try:
        import fund_screener
        key = f"fundscreen:{type}:{sort_by}:{top_n}:{min_1y}:{min_3y}:{max_fee}"
        out = _cache_or(key, 3600,
                        lambda: fund_screener.screen_funds(type, sort_by, top_n,
                                                           min_1y=min_1y, min_3y=min_3y, max_fee=max_fee),
                        keep=lambda v: bool(v))
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/fund/valuation")
def fund_valuation_pe(index: str = "沪深300"):
    """宽基指数估值分位(滚动PE)→ 档位 + 定投倍数。Redis 缓存 6h。"""
    try:
        import fund_valuation
        out = _cache_or(f"fundval:{index}", 21600,
                        lambda: fund_valuation.index_pe_percentile(index, None),
                        keep=lambda v: isinstance(v, dict) and v.get("pe"))
        return _ok(out or {"error": "估值获取失败", "index": index})
    except Exception as e:
        return _err(e)


@app.get("/api/fund/compare")
def fund_compare(codes: str = "", lookback_days: int = 0):
    """多只基金并排对比(同一共同时间窗):年化/总收益/回撤/夏普/卡玛/波动 + 归一净值叠加曲线。
    codes 逗号分隔(2-6 只为宜);lookback_days>0 只比最近 N 天。Redis 缓存 1h。"""
    try:
        import fund_analysis
        cs = [c.strip() for c in codes.replace("，", ",").split(",") if c.strip()]
        if len(cs) < 2:
            return _err("请至少提供 2 只基金代码(逗号分隔)")
        lb = lookback_days or None
        key = f"fundcompare:{','.join(sorted(cs))}:{lb}"
        out = _cache_or(key, 3600, lambda: fund_analysis.compare_funds(cs, lookback_days=lb),
                        keep=lambda v: isinstance(v, dict) and bool(v.get("funds")))
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/fund/{code}/extras")
def fund_extras(code: str):
    """单只基金档案:前十大重仓股(最新季度)+ 评级摘要(经理/公司/机构星级/费率)+ 基本信息。
    3 个网络调用并发,**各自独立缓存 6h 且仅成功才缓存**(某源瞬时失败不会被缓存,下次自动重试)。"""
    import fund_data
    from concurrent.futures import ThreadPoolExecutor
    nonempty = lambda v: isinstance(v, dict) and bool(v)
    def th():
        return _cache_or(f"fundth:{code}", 21600, lambda: fund_data.top_holdings(code, 10),
                         keep=lambda v: isinstance(v, dict) and bool(v.get("holdings")))
    def rt():
        return _cache_or(f"fundrt:{code}", 21600, lambda: fund_data.rating_summary(code), keep=nonempty)
    def ba():
        return _cache_or(f"fundba:{code}", 21600, lambda: fund_data.get_fund_basic(code), keep=nonempty)
    try:
        with ThreadPoolExecutor(max_workers=3) as exr:
            fth, frt, fba = exr.submit(th), exr.submit(rt), exr.submit(ba)
            return _ok({"top_holdings": fth.result() or {"holdings": []},
                        "rating": frt.result() or {}, "basic": fba.result() or {}})
    except Exception as e:
        return _err(e)


@app.delete("/api/fund/plan/{plan_id}")
def fund_delete_plan(plan_id: int):
    """删除定投计划。"""
    try:
        import fund_db
        fund_db.init_db()
        fund_db.delete_plan(plan_id)
        return _ok({"deleted": plan_id})
    except Exception as e:
        return _err(e)


@app.post("/api/fund/plan/{plan_id}/toggle")
def fund_toggle_plan(plan_id: int):
    """启用/停用定投计划(翻转当前状态)。"""
    try:
        import fund_db
        fund_db.init_db()
        cur = next((p for p in fund_db.get_plans() if int(p.get("id")) == plan_id), None)
        if not cur:
            return _err("计划不存在")
        new_state = not bool(cur.get("enabled"))
        fund_db.set_plan_enabled(plan_id, new_state)
        return _ok({"id": plan_id, "enabled": new_state})
    except Exception as e:
        return _err(e)


@app.get("/api/fund/diagnose")
def fund_diagnose(overlap: bool = False):
    """基金组合诊断:大类/类型配置权重、集中度(HHI/top1/top3)、(可选)重仓股穿透重叠 + 建议。
    市值用库内净值×份额估算(避免逐只联网);overlap=1 才做重仓穿透(慢,53只逐只抓持仓)。"""
    try:
        import fund_db, fund_portfolio
        fund_db.init_db()
        holdings = fund_db.get_holdings()
        if not holdings:
            return _ok({"error": "无持有基金"})
        # 用库内净值算市值,避免 diagnose 内逐只 latest_nav 联网(53只省~7s)
        codes = [str(h.get("code")) for h in holdings]
        navs = fund_db.get_latest_navs(codes)
        enriched = []
        for h in holdings:
            code = str(h.get("code"))
            shares = float(h.get("shares") or 0)
            unit = float((navs.get(code) or {}).get("unit_nav") or h.get("cost_nav") or 0)
            enriched.append({**h, "code": code, "market_value": round(shares * unit, 2)})
        return _ok(fund_portfolio.diagnose(enriched, with_overlap=overlap))
    except Exception as e:
        return _err(e)


@app.get("/api/fund/combined-view")
def fund_combined_view():
    """股票 + 基金 大类资产合并视图(成本口径,offline)。各大类金额与占比。"""
    try:
        import fund_portfolio
        return _ok(fund_portfolio.combined_asset_view())
    except Exception as e:
        return _err(e)


@app.post("/api/fund/{code}/ai-panel")
def fund_ai_panel(code: str):
    """基金多角色 AI 研判面板(业绩/风险/定投适配 + 综合)。⚠️ 需 LLM key、耗 token。"""
    try:
        import fund_analysis
        return _ok(fund_analysis.ai_research_panel(code))
    except Exception as e:
        return _err(e)


# 放在静态 /api/fund/* 路由之后(见上方注释:避免 {code} 捕获 holdings/transactions/plans)
@app.get("/api/fund/{code}")
def fund_info(code: str):
    """基金概览。原 4 个网络调用串行(~8s)→ 并发 + Redis 缓存 5min。"""
    def compute():
        import fund_data
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            fname = ex.submit(fund_data.fund_name, code)
            ftype = ex.submit(fund_data.fund_type, code)
            latest = ex.submit(fund_data.latest_nav, code)
            rt = ex.submit(fund_data.get_realtime_estimate, code)
            return {"code": code, "name": fname.result(), "type": ftype.result(),
                    "latest": latest.result(), "realtime": rt.result()}
    try:
        return _ok(_cache_or(f"fundinfo:{code}", 300, compute, keep=lambda v: isinstance(v, dict)))
    except Exception as e:
        return _err(e)


# ============================ 持仓/组合 ============================
@app.get("/api/portfolio/overview")
def portfolio_overview():
    """股票+基金大类资产合并视图(成本口径)。Redis 缓存 10min。"""
    try:
        import fund_portfolio
        return _ok(_cache_or("pf_overview", 600, fund_portfolio.combined_asset_view,
                             keep=lambda v: isinstance(v, dict)))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/daily-pnl")
def portfolio_daily_pnl(days: int = 30):
    """合并日收益(股票+基金):汇总(今日/本月/区间累计/胜率/最佳最差) + 近N日序列。
    数据=daily_pnl_snapshots(每日22:30收盘口径)。盘中实时今日盈亏见持仓页前端汇总。"""
    try:
        from portfolio.daily_pnl import get_summary, get_recent
        return _ok({"summary": get_summary(max(days, 60)), "series": get_recent(days)})
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/stocks")
def portfolio_stocks():
    try:
        from portfolio_db import portfolio_db
        # 只展示在持(quantity>0);qty=0 的清仓股保留在库做历史,但不在持仓列表显示
        stocks = [s for s in (portfolio_db.get_all_stocks() or [])
                  if float(s.get("quantity") or s.get("shares") or 0) > 0]
        codes = [str(s.get("code")) for s in stocks if s.get("code")]

        def _fetch():
            import datahub
            return datahub.quotes(codes) if codes else {}

        # 行情 8s 缓存(刷新去重)+ 8s 超时护栏(盘外/弱网拉不到就用成本价兜底,页面不卡死)
        quotes = _cache_or(f"pf_quotes:{','.join(sorted(codes))}", 8,
                           lambda: _with_deadline(_fetch, 8, {}),
                           keep=lambda v: bool(v)) if codes else {}
        out = []
        for s in stocks:
            code = str(s.get("code"))
            qty = float(s.get("quantity") or s.get("shares") or 0)
            cost = float(s.get("cost_price") or s.get("cost") or 0)
            q = quotes.get(code) or {}
            price = q.get("price") or cost
            # 适配器返回字段是 last_close(昨收);原取 prev_close 永远 None→退回成本价,今日涨跌算错
            prev_close = q.get("last_close") or q.get("prev_close") or cost
            change_pct = round((price - prev_close) / prev_close, 4) if prev_close else None
            # 今日收益 = 每股涨跌 × 持仓数量(持仓口径,直观);原来只给每股涨跌额、看着不直观
            today_change = round(qty * (price - prev_close), 2) if prev_close else None
            out.append({"code": code, "name": s.get("name"), "qty": qty, "cost": cost,
                        "price": price, "mv": round(qty * price, 2),
                        "pnl_pct": round((price - cost) / cost, 4) if cost else None,
                        "today_change": today_change, "today_change_pct": change_pct,
                        "quotes_ok": bool(q)})
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/stress")
def portfolio_stress(include_funds: bool = True):
    """情景压力测试(股票+基金)。净值读库,Redis 缓存 10min(持仓/净值日内变化有限)。"""
    def compute():
        import scenario_stress
        pos = scenario_stress.build_portfolio_positions(include_funds=include_funds)
        if not pos:
            return []
        return [{"scenario": r["scenario"], "pnl_pct": r["total_pnl_pct"], "pnl": r["total_pnl"]}
                for r in scenario_stress.stress_all(pos)]
    try:
        # 冷态首访需逐建持仓(含批量行情),套 15s 护栏防外部源慢时卡死(超时返回空,下轮缓存补上)
        return _ok(_cache_or(f"pf_stress:{int(include_funds)}", 600,
                             lambda: _with_deadline(compute, 15, []), keep=lambda v: bool(v)))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/curve")
def portfolio_curve():
    try:
        import portfolio_snapshot
        return _ok(portfolio_snapshot.get_snapshots())
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/trade-records")
def portfolio_trade_records(code: str = "", ttype: str = "", days: int = 30):
    """成交记录：按代码/方向/天数筛选"""
    try:
        from core.database_pg import get_conn
        conn = get_conn()
        cur = conn.cursor()
        where = []
        params = []
        if code:
            codes = [c.strip() for c in code.split(",") if c.strip()]
            if len(codes) == 1:
                where.append("stock_code = %s")
                params.append(codes[0])
            else:
                where.append("stock_code = ANY(%s)")
                params.append(codes)
        if ttype:
            where.append("trade_type = %s")
            params.append(ttype)
        if days:
            where.append("trade_time >= now() - interval %s")
            params.append(f"{days} days")
        clause = " AND ".join(where) if where else "TRUE"
        cur.execute(f"""
            SELECT id, stock_code, stock_name, trade_type, price, quantity, amount,
                   pos_quantity, pos_cost_price, source, note, trade_time,
                   commission, profit_loss
            FROM trade_records
            WHERE {clause}
            ORDER BY trade_time DESC
            LIMIT 200
        """, params)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        out = [{
            "id": r[0], "stock_code": r[1], "stock_name": r[2], "trade_type": r[3],
            "price": float(r[4]) if r[4] else None, "quantity": r[5],
            "amount": float(r[6]) if r[6] else None, "pos_quantity": r[7],
            "pos_cost_price": float(r[8]) if r[8] else None, "source": r[9],
            "note": r[10], "trade_time": str(r[11])[:19] if r[11] else "",
            "commission": float(r[12]) if r[12] else None,
            "profit_loss": float(r[13]) if r[13] else None,
        } for r in rows]
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/performance")
def portfolio_performance():
    """组合绩效:TWR(时间加权)/XIRR(资金加权年化)/波动/最大回撤/夏普/盈亏归因。
    全用现成净值快照+成交记录,无外部行情拉取。Redis 缓存 10min。"""
    try:
        from performance import summary
        return _ok(_cache_or("pf_perf:v2", 600, summary, keep=lambda v: isinstance(v, dict)))
    except Exception as e:
        return _err(e)


@app.get("/api/factors/eval")
def factors_eval(horizon: int = 10):
    """因子IC评估:RankIC/IC-IR/胜率/随机对照,科学衡量价量因子预测力。
    需拉股池K线较慢→25s护栏+24h缓存(因子日级,周任务 factor_eval 会预热)。"""
    try:
        from factor_eval import evaluate
        return _ok(_cache_or(f"factor_eval:{horizon}", 86400,
                             lambda: _with_deadline(lambda: evaluate(horizon=horizon), 25, {"factors": [], "error": "timeout(等周任务预热)"}),
                             keep=lambda v: isinstance(v, dict) and v.get("factors")))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/optimize")
def portfolio_optimize(codes: str = "", period: str = "6mo", method: str = ""):
    """组合权重优化:对给定 codes(逗号分隔)算 等权/逆波动/最小方差/风险平价 配比。
    codes 留空则用当前持仓。K线拉取慢 → 20s 护栏。Redis 缓存 10min。"""
    try:
        from optimizer import optimize
        clist = [c.strip() for c in codes.split(",") if c.strip()]
        if not clist:
            # 用当前持仓:按成本市值取 top15(优化器适合精选短名单,68只微仓既无意义又拉不动K线)
            from portfolio_db import portfolio_db
            hs = [h for h in (portfolio_db.get_all_stocks() or [])
                  if h.get("code") and float(h.get("quantity") or 0) > 0]
            hs.sort(key=lambda h: float(h.get("quantity") or 0) * float(h.get("cost_price") or h.get("cost") or 0), reverse=True)
            clist = [str(h.get("code")) for h in hs[:15]]
        methods = [method] if method else None
        key = f"pf_opt:{','.join(sorted(clist))}:{period}:{method}"
        return _ok(_cache_or(key, 600,
                             lambda: _with_deadline(lambda: optimize(clist, period, methods), 20,
                                                    {"error": "timeout", "used": [], "dropped": clist}),
                             keep=lambda v: isinstance(v, dict) and v.get("used")))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/benchmark")
def portfolio_benchmark(code: str = "000300"):
    """组合 vs 基准指数(默认沪深300)累计收益对比 + 超额。Redis 缓存 10min。"""
    try:
        from benchmark import compare
        # 指数历史拉取可能慢(akshare),12s 护栏不让页面卡死;成功结果缓存 1h(日级数据)
        return _ok(_cache_or(f"pf_bench:{code}", 3600,
                             lambda: _with_deadline(lambda: compare(code), 12, {"dates": [], "error": "timeout"}),
                             keep=lambda v: isinstance(v, dict) and v.get("dates")))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/montecarlo")
def portfolio_montecarlo(horizon: int = 60):
    """组合蒙特卡洛预测:bootstrap 持仓加权历史日收益模拟未来路径,出分位区间/亏损概率/VaR。
    需拉重仓K线(已磁盘缓存)→20s护栏+缓存30min。"""
    try:
        from monte_carlo import simulate
        return _ok(_cache_or(f"pf_mc:{horizon}", 1800,
                             lambda: _with_deadline(lambda: simulate(horizon=horizon), 20, {"error": "timeout"}),
                             keep=lambda v: isinstance(v, dict) and not v.get("error")))
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/xray")
def portfolio_xray():
    """组合体检(X-Ray):规则化健康度评估(集中度/质地/风险/结构)。
    行情走 8s 超时护栏 + 与 /portfolio/stocks 同缓存键(刷过持仓页则秒回);拉不到用成本价兜底。"""
    try:
        from portfolio_db import portfolio_db
        from portfolio_rules import evaluate
        holdings = [h for h in (portfolio_db.get_all_stocks() or [])
                    if isinstance(h, dict) and float(h.get("quantity") or h.get("shares") or 0) > 0]
        codes = [str(h.get("code")) for h in holdings if h.get("code")]

        def _fetch():
            import datahub
            out = {}
            for i in range(0, len(codes), 20):
                out.update(datahub.quotes(codes[i:i + 20]) or {})
            return out

        quotes = _cache_or(f"pf_quotes:{','.join(sorted(codes))}", 8,
                           lambda: _with_deadline(_fetch, 8, {}),
                           keep=lambda v: bool(v)) if codes else {}
        return _ok(_cache_or("pf_xray", 300, lambda: evaluate(holdings, quotes),
                             keep=lambda v: isinstance(v, dict) and v.get("n")))
    except Exception as e:
        return _err(e)


@app.get("/api/trades/behavior")
def trades_behavior():
    """交易行为诊断(影子账户):从成交记录FIFO配对回合,诊断处置效应/止损纪律/盈亏比/过度交易。
    纯算成交记录,无外部拉取。Redis 缓存 10min。"""
    try:
        from shadow_account import run_diagnose
        return _ok(_cache_or("trade_behavior:v2", 600, run_diagnose,
                             keep=lambda v: isinstance(v, dict) and not v.get("error")))
    except Exception as e:
        return _err(e)


@app.get("/api/trades/realized")
def trades_realized(days: int = 0):
    """已实现盈亏汇总(卖出笔):总盈亏/胜率/盈亏比/按股票。days=0 为累计。
    顺手回填 PG trade_records.profit_loss(幂等),成交记录页盈亏列即点亮。"""
    try:
        from portfolio.realized_pnl import backfill, summary
        try:
            backfill()
        except Exception:
            pass
        return _ok(summary(days=days or None))
    except Exception as e:
        return _err(e)


@app.post("/api/portfolio/snapshot")
def portfolio_snapshot_now():
    """手动按当前持仓+实时价落一条组合净值快照(幂等覆盖当天)。无持仓返回提示。"""
    try:
        import datetime
        import portfolio_snapshot
        today = datetime.date.today().isoformat()
        r = portfolio_snapshot.save_snapshot(today)
        if not r:
            return _ok({"saved": False, "msg": "当前无股票持仓"})
        return _ok({"saved": True, **r})
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/signals")
def portfolio_signals_api(limit: int = 20):
    """持仓操作信号(on-demand):加仓审核(guardian:触发跌幅→质地审核 approve/reject)+
    减仓信号(profit_taker:30/60/100%阶梯 + 跌破MA20/60 保护 + 情绪过热)。
    加/减仓两路**并发**扫描(走常驻池,不被 hang 住的冷门股阻塞响应),各只扫市值前 limit 只(默认20),
    各 45s 截止护栏。Redis 缓存 10min。"""
    def compute():
        import position_guardian as pg
        import position_profit_taker as pt
        from concurrent.futures import wait as _cf_wait
        # 用模块级常驻池(_DEADLINE_POOL):其不会在退出时 shutdown(wait=True) 阻塞,
        # 超时即放弃(被弃线程后台跑完丢弃),响应不被个别卡死的行情源拖住。
        fa = _DEADLINE_POOL.submit(pg.evaluate_all_triggered, 8, limit, False)  # 跳过慢基本面
        fr = _DEADLINE_POOL.submit(pt.evaluate_all, 8, limit)
        # 单一 45s 预算等两路(wait 一起等,避免 fa.result(45)+fr.result(45) 叠加成 90s)
        done, _ = _cf_wait([fa, fr], timeout=45)

        def _res(f):
            try:
                return f.result() if f in done else None
            except Exception:
                return None
        add, reduce = _res(fa), _res(fr)
        if add is None and reduce is None:
            return None
        # add_ok/reduce_ok:该路是否扫完(超时=False)→ 前端区分"无信号"与"扫描超时,别误读 0"。
        # _complete=两路都成功才缓存:某路超时的"部分结果"不缓存(否则 add=[] 被当真无信号缓存10min)
        return {"add": add or [], "reduce": reduce or [],
                "add_count": len(add or []), "reduce_count": len(reduce or []),
                "add_ok": add is not None, "reduce_ok": reduce is not None,
                "_complete": (add is not None and reduce is not None)}
    try:
        out = _cache_or(f"portfolio:signals:{limit}", 600, compute,
                        keep=lambda v: isinstance(v, dict) and v.get("_complete"))
        if out is None:
            return _err("信号扫描超时(部分持仓行情源较慢),请稍后重试")
        out.pop("_complete", None)
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/insights")
def portfolio_insights_api(since_days: int = 90):
    """持仓交易习惯洞察(纯库读,秒回):持有时长分布 + 交易频次/买卖比 + 变动时间线/最活跃股。"""
    try:
        import portfolio_insights as pi
        return _ok({
            "duration": pi.holding_duration_distribution(),
            "frequency": pi.trading_frequency_analysis(since_days=since_days),
            "timeline": pi.portfolio_change_timeline(since_days=since_days, limit=200),
        })
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/classify")
def portfolio_classify_api(limit: int = 15, fundamental: bool = False):
    """持仓自动分级(健康🟢/观察🟡/警报🔴/数据不足⚪),基于 持仓盈亏+趋势(MA)+看跌反转形态。
    limit 只扫市值最大的前 N 只(默认30)。fundamental=true 才加基本面评分(每只2-4s且依赖pywencai/F10,
    弱网/未配置时返回N/A纯耗时,默认关→秒级)。Redis 缓存 30min。"""
    try:
        import portfolio_classifier as pc
        # 逐只走实时报价/K线,个别冷门股外部源会卡 → 45s 截止护栏,超时返回提示而非挂死(已算的进缓存下次秒回)
        out = _cache_or(
            f"portfolio:classify:{limit}:{fundamental}", 1800,
            lambda: _with_deadline(lambda: pc.classify_all(limit=limit, with_fundamental=fundamental), 45, None),
            keep=lambda v: isinstance(v, dict) and any(v.get(k) for k in ("healthy", "watch", "alert", "na")))
        if not out:
            return _err("分级超时(部分持仓行情源较慢),请调小 limit 或稍后重试")
        counts = {k: len(out.get(k, [])) for k in ("healthy", "watch", "alert", "na")}
        return _ok({"counts": counts, "by_class": out, "limit": limit, "with_fundamental": fundamental})
    except Exception as e:
        return _err(e)


@app.get("/api/portfolio/diagnose-ai")
def portfolio_diagnose_ai():
    """AI 持仓诊断(LLM):基于 估值/变动/持有时长/交易频次 4 报表,给交易习惯+风险+改进建议+风险/纪律评分。
    ⚠️ 需 LLM key;**无 key/超时不阻塞**——仍返回 context(规则报表),diagnosis 段标注失败。
    LLM 已有 per-call 超时,这里再套 60s 截止护栏。仅 LLM 成功才缓存(30min),失败下次重试。"""
    try:
        import portfolio_insights as pi
        out = _cache_or(
            "portfolio:diagnose_ai", 1800,
            lambda: _with_deadline(pi.diagnose_portfolio, 60, None),
            keep=lambda v: isinstance(v, dict) and isinstance(v.get("diagnosis"), dict)
            and "error" not in v["diagnosis"] and "raw_text" not in v["diagnosis"])
        if out is None:
            return _err("AI 诊断超时,请稍后重试(规则报表可看交易习惯洞察卡片)")
        return _ok(out)
    except Exception as e:
        return _err(e)


# ============================ 市场 ============================
@app.get("/api/market/north")
def market_north(days: int = 30):
    try:
        import datahub
        out = _cache_or(f"north:{days}", 1800,
                        lambda: _records(datahub.north_flow(days)), keep=lambda v: bool(v))
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.post("/api/market/north-refresh")
def market_north_refresh():
    """手动触发北向资金实时拉取（同花顺 hsgtApi）"""
    try:
        from northbound_cache import refresh_today, get_recent
        data = refresh_today()
        if data:
            return _ok({"refreshed": True, "date": data["date"],
                        "hgt": data["hgt_yi"], "sgt": data["sgt_yi"],
                        "note": "北向资金自2024年8月起多个数据源断供，若数值多日不变为正常现象"})
        recent = get_recent(1)
        return _ok({"refreshed": False, "note": "同花顺API返回空或与上次相同",
                     "last": recent[0]["trade_date"] if recent else "无"})
    except Exception as e:
        return _err(e)


def _recent_trade_dates(n: int = 10):
    """最近 n 个交易日(YYYY-MM-DD,降序)。Redis 缓存 1 天。"""
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    if cache_get:
        hit = cache_get("trade_dates")
        if hit:
            return hit[:n]
    try:
        import akshare as ak
        import datetime
        df = ak.tool_trade_date_hist_sina()
        col = df.columns[0]
        today = datetime.date.today().isoformat()
        ds = sorted([str(d) for d in df[col] if str(d) <= today], reverse=True)[:30]
        if cache_set:
            cache_set("trade_dates", ds, ttl=86400)
        return ds[:n]
    except Exception:
        return []


@app.get("/api/market/trade-dates")
def market_trade_dates():
    return _ok(_recent_trade_dates(10))


def _lhb_range():
    """最近 14 天龙虎榜明细(records)。Redis 缓存 1 小时(整段抓取较慢)。"""
    try:
        import sys as _s
        _s.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
        from cache import cache_get, cache_set
    except Exception:
        cache_get = cache_set = None
    if cache_get:
        hit = cache_get("lhb_range")
        if hit is not None:
            return hit
    import akshare as ak
    import datetime
    end = datetime.date.today()
    start = end - datetime.timedelta(days=14)
    df = ak.stock_lhb_detail_em(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    rows = _records(df) if df is not None and not df.empty else []
    if cache_set:
        cache_set("lhb_range", rows, ttl=3600)
    return rows


@app.get("/api/market/dragon")
def market_dragon(date: str = ""):
    """龙虎榜:某交易日完整榜单(默认最近一天),按净买额排序。date 形如 2026-06-05。"""
    try:
        rows = _lhb_range()
        if not rows:
            return _ok([])
        dkey = next((k for k in rows[0] if "上榜日" in k or "日期" in k), None)
        if dkey:
            target = date or max(str(r.get(dkey)) for r in rows)
            rows = [r for r in rows if str(r.get(dkey)) == target]
        nkey = next((k for k in (rows[0] if rows else {}) if "净买额" in k), None)
        if nkey:
            rows.sort(key=lambda r: (r.get(nkey) is not None, r.get(nkey) or 0), reverse=True)
        for r in rows:
            r.pop("序号", None)
            r.pop("市场总成交额", None)
        return _ok(rows[:80])
    except Exception as e:
        return _err(e)


@app.get("/api/market/lhb-inst")
def market_lhb_institution(days: int = 14):
    """龙虎榜·机构动向(smart money):近 N 天机构买卖统计,按机构净买额降序。
    替代旧"智瞰龙虎"(原走东财push2席位数据,被墙);本源稳定可达。Redis 缓存 1h。"""
    def _jgmm():   # 主源:东财机构买卖统计(含机构数/净买额)
        import akshare as ak
        import datetime
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
        df = ak.stock_lhb_jgmmtj_em(start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
        rows = [{
            "代码": r.get("代码"), "名称": r.get("名称"), "涨跌幅": _f(r.get("涨跌幅")),
            "买方机构数": _f(r.get("买方机构数")), "卖方机构数": _f(r.get("卖方机构数")),
            "机构买入净额": _f(r.get("机构买入净额")), "机构净买占比%": _f(r.get("机构净买额占总成交额比")),
            "换手率": _f(r.get("换手率")), "上榜原因": r.get("上榜原因"), "上榜日期": str(r.get("上榜日期"))[:10],
        } for _, r in df.iterrows()]
        rows.sort(key=lambda x: (x.get("机构买入净额") is not None, x.get("机构买入净额") or -1e18, str(x.get("上榜日期"))), reverse=True)
        return rows[:120]

    def _detail():   # 备源:龙虎榜明细(无机构拆分,降级为净买额排名)
        rows = _lhb_range()
        if not rows:
            return []
        nkey = next((k for k in rows[0] if "净买额" in k), None)
        out = [{"代码": r.get("代码"), "名称": r.get("名称"),
                "净买额": _f(r.get(nkey)) if nkey else None,
                "上榜原因": r.get("解读") or r.get("上榜原因"), "上榜日期": str(r.get(next((k for k in r if "上榜日" in k), ""), ""))[:10]}
               for r in rows]
        out.sort(key=lambda x: (x.get("净买额") is not None, x.get("净买额") or -1e18, str(x.get("上榜日期"))), reverse=True)
        return out[:120]

    try:
        return _ok(_cache_or(f"lhbinst:{days}", 3600, lambda: _try(_jgmm, _detail), keep=lambda v: bool(v)))
    except Exception as e:
        return _err(e)


@app.get("/api/market/lhb-ai")
def market_lhb_ai(days: int = 14):
    """龙虎榜机构/游资 AI 解读:把机构买卖榜喂给 LLM,解读资金动向 + 值得关注标的。
    ⚠️ 需 LLM key。Redis 缓存 30min。"""
    def compute():
        from cache import cache_get
        rows = cache_get(f"lhbinst:{days}") or []
        if not rows:
            return {"error": "龙虎榜数据为空,请先打开龙虎榜·机构页"}
        top = rows[:18]
        lines = []
        for r in top:
            net = r.get("机构买入净额")
            netstr = f"{net/1e8:.2f}亿" if isinstance(net, (int, float)) else (str(r.get("净买额", "")) or "—")
            lines.append(f"{r.get('名称')}({r.get('代码')}) 涨跌{r.get('涨跌幅','—')}% 机构净买{netstr} "
                         f"买方机构{r.get('买方机构数','—')}/卖方{r.get('卖方机构数','—')} {r.get('上榜原因','')}")
        user = "近期龙虎榜机构买卖(按机构净买额排序):\n" + "\n".join(lines) + \
               "\n\n基于以上,简洁给出:① 机构资金主攻方向/题材 ② 机构高度共振(多买方机构)的标的 ③ 疑似游资博弈(高换手/单边)的标的 ④ 风险提示。用数据,别空话。"
        return _llm_analyze("你是A股龙虎榜资金分析师,区分机构与游资风格,输出简洁中文要点(markdown)。", user, max_tokens=900)
    try:
        return _ok(_cache_or(f"lhb_ai:{days}", 1800, compute, keep=lambda v: isinstance(v, dict) and v.get("analysis")))
    except Exception as e:
        return _err(e)


@app.get("/api/market/hot")
def market_hot(date: str = ""):
    """热门题材/强势股。date 形如 2026-06-05(缺省最新)。Redis 缓存 30min。"""
    try:
        import datahub
        out = _cache_or(f"hot:{date or 'latest'}", 1800,
                        lambda: _records(datahub.hot_stocks(date or None)), keep=lambda v: bool(v))
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/market/indices")
def market_indices():
    """主要大盘指数实时(上证/深证/创业板/科创50/沪深300/恒生)。
    2026-06-12:新浪→腾讯源链已收进统一数据层 datahub.indices;此处只缓存。Redis 缓存 20s。"""
    try:
        import datahub
        return _ok(_cache_or("market_indices", 20, datahub.indices, keep=lambda v: bool(v)))
    except Exception as e:
        return _err(e)


def _flash_news_rows(limit=60):
    """全市场财经快讯(东财全球快讯→财联社兜底)。统一走 datahub.market_news。
    返回 [{title,summary,time,url}](summary 兼容旧前端键)。"""
    import datahub
    return [{"title": n.get("title", ""), "summary": n.get("content", ""),
             "time": n.get("time", ""), "url": n.get("url", "")}
            for n in datahub.market_news(limit)]


# 财经情绪关键词(借自 news_flow_sentiment)
_POS_KW = ['利好', '大涨', '暴涨', '涨停', '新高', '突破', '牛市', '反弹', '加仓', '买入',
           '增持', '推荐', '看好', '机遇', '政策支持', '业绩预增', '超预期', '景气度', '高增长']
_NEG_KW = ['利空', '大跌', '暴跌', '跌停', '新低', '破位', '熊市', '回调', '减仓', '卖出',
           '减持', '风险', '看空', '危机', '政策收紧', '业绩下滑', '不及预期', '亏损', '退市']


@app.get("/api/market/news")
def market_flash_news(limit: int = 60):
    """全市场财经快讯(东财全球快讯→财联社兜底)。Redis 缓存 5min。"""
    try:
        return _ok(_cache_or("market_flash", 300, lambda: _flash_news_rows(limit), keep=lambda v: bool(v)))
    except Exception as e:
        return _err(e)


@app.get("/api/market/news-sentiment")
def market_news_sentiment():
    """财经快讯情绪:对快讯标题+摘要做正负面关键词计数 → 情绪指数 0-100 + 倾向。
    复用财经快讯缓存,不额外抓取。"""
    def compute():
        news = _cache_or("market_flash", 300, lambda: _flash_news_rows(60), keep=lambda v: bool(v)) or []
        pos = neg = 0
        pos_hits, neg_hits = [], []
        for n in news:
            text = (n.get("title", "") or "") + (n.get("summary", "") or "")
            p = sum(1 for k in _POS_KW if k in text)
            q = sum(1 for k in _NEG_KW if k in text)
            if p > q and n.get("title"):
                pos += 1; pos_hits.append(n["title"][:40])
            elif q > p and n.get("title"):
                neg += 1; neg_hits.append(n["title"][:40])
            else:
                pos += p; neg += q   # 仅计数,不归类
        total = pos + neg
        ratio = (pos / total) if total else 0.5
        index = int(round(ratio * 100))
        cls_ = "乐观" if index >= 60 else ("谨慎" if index <= 40 else "中性")
        return {"index": index, "class": cls_, "positive": pos, "negative": neg,
                "total_news": len(news), "pos_samples": pos_hits[:5], "neg_samples": neg_hits[:5]}
    try:
        return _ok(_cache_or("market_news_senti", 300, compute, keep=lambda v: isinstance(v, dict)))
    except Exception as e:
        return _err(e)


@app.get("/api/market/news-ai")
def market_news_ai():
    """财经快讯 AI 研判:把快讯喂给 LLM,提炼市场情绪/热点主题/利好利空。⚠️ 需 LLM key。缓存 30min。"""
    def compute():
        news = _cache_or("market_flash", 300, lambda: _flash_news_rows(60), keep=lambda v: bool(v)) or []
        if not news:
            return {"error": "快讯为空"}
        titles = [("- " + (n.get("title") or "")) for n in news[:40] if n.get("title")]
        user = "今日财经快讯标题(约40条):\n" + "\n".join(titles) + \
               "\n\n基于以上快讯,简洁给出:① 当前市场情绪(乐观/中性/谨慎,一句话理由)② 三大热点主题/题材 ③ 受益(利好)方向 ④ 承压(利空)方向 ⑤ 一句话操作提示。用要点,别堆砌原文。"
        return _llm_analyze("你是A股财经新闻分析师,输出简洁中文要点(markdown)。", user, max_tokens=900)
    try:
        return _ok(_cache_or("news_ai", 1800, compute, keep=lambda v: isinstance(v, dict) and v.get("analysis")))
    except Exception as e:
        return _err(e)


@app.get("/api/market/news/{code}")
def market_news(code: str):
    """个股新闻。Redis 缓存 10min。"""
    try:
        import datahub
        out = _cache_or(f"stocknews:{code}", 600,
                        lambda: _records(datahub.stock_news(code)), keep=lambda v: bool(v))
        return _ok(out)
    except Exception as e:
        return _err(e)


# ============================ 选股 ============================
@app.get("/api/screen/multifactor")
def screen_multifactor(index: str = "000300", n: int = 15, refresh: bool = False,
                       style: str = "balanced"):
    """多因子选股。Redis 缓存同指数"已打分池"6h(并发取因子);首算较慢、之后秒回。
    style: balanced/value/growth/quality/dividend —— 同一缓存池上重新加权,切换零成本。
    jobs 周扫(wf_multi_factor_screen)会预热同一缓存键。refresh=1 强制现算(仅盘后生效)。

    ⚠️ 2026-06-27 防东财封禁:**盘中(交易时段)一律 cache_only**——只读盘后 16:30 焐好的缓存池,
    冷则返回空(cache_only_miss),且**盘中忽略 refresh/force**(用户连点"强制刷新"不会在交易时段
    现拉 ~87 次东财板块成分接口 + 60 只因子)。强制刷新只在盘后允许,与盘中定时选股(cache_only)对齐。"""
    try:
        from multi_factor_screener import screen_index_cached
        try:
            from datahub import _is_trading_hours
            _trading = _is_trading_hours()
        except Exception:
            _trading = False
        r = screen_index_cached(index_code=index, n=n, add_sector_leaders=True,
                                workers=8, force=(bool(refresh) and not _trading),
                                style=style, cache_only=_trading)
        if "top" not in r:
            return _err(r.get("error", "选股失败"))
        return _ok(r)
    except Exception as e:
        return _err(e)


class RecipeReq(BaseModel):
    recipe: str
    codes: list[str]


@app.post("/api/screen/recipe")
def screen_recipe_ep(req: RecipeReq):
    """对候选池跑内置选股配方(290条件库的6个配方)。"""
    try:
        from screener_engine import screen_recipe, RECIPES
        if req.recipe not in RECIPES:
            return _err(f"未知配方,可选: {list(RECIPES.keys())}")
        codes = [str(c).strip() for c in (req.codes or []) if str(c).strip()]
        if not codes:
            return _err("候选池为空")
        return _ok(screen_recipe(req.recipe, codes, verbose=False))
    except Exception as e:
        return _err(e)


class RecoReq(BaseModel):
    codes: list[str]


@app.post("/api/reco/dual-horizon")
def reco_dual_horizon(req: RecoReq):
    """AI 双层推荐:候选池 → 短线(动量≥70)/长期(基本面≥75)两档。⚠️ 需 LLM key、耗 token。"""
    try:
        import dual_horizon_reco as dhr
        codes = [str(c).strip() for c in (req.codes or []) if str(c).strip()]
        if not codes:
            return _err("候选池为空")
        if len(codes) > 40:
            codes = codes[:40]
        return _ok(dhr.recommend(codes))
    except Exception as e:
        return _err(e)


# ============================ 板块 ============================
@app.get("/api/sector/board")
def sector_board(source: str = "sina"):
    """板块强弱(非东财push2源,稳定)。source: sina(新浪行业,快,带领涨股)/ths(同花顺行业,带净流入)。
    Redis 缓存 10min。"""
    def _sina():
        import akshare as ak
        df = ak.stock_sector_spot(indicator="新浪行业")
        rows = [{"板块": r.get("板块"), "涨跌幅": _f(r.get("涨跌幅")),
                 "公司家数": _f(r.get("公司家数")), "总成交额": _f(r.get("总成交额")),
                 "领涨股": r.get("股票名称"), "领涨幅": _f(r.get("个股-涨跌幅"))} for _, r in df.iterrows()]
        return _sorted(rows)

    def _ths():
        import akshare as ak
        df = ak.stock_board_industry_summary_ths()
        rows = [{"板块": r.get("板块"), "涨跌幅": _f(r.get("涨跌幅")), "净流入": _f(r.get("净流入")),
                 "总成交额": _f(r.get("总成交额")), "总成交量": _f(r.get("总成交量"))} for _, r in df.iterrows()]
        return _sorted(rows)

    def _sorted(rows):
        rows.sort(key=lambda x: (x.get("涨跌幅") is not None, x.get("涨跌幅") or -1e9), reverse=True)
        return rows

    primary, backup = (_ths, _sina) if source == "ths" else (_sina, _ths)
    try:   # 主源挂了自动换备源
        return _ok(_cache_or(f"sector:{source}", 600, lambda: _try(primary, backup), keep=lambda v: bool(v)))
    except Exception as e:
        return _err(e)


@app.get("/api/sector/ai-rotation")
def sector_ai_rotation():
    """板块轮动 AI 研判:把行业涨跌+资金流喂给 LLM,产出轮入/轮出主线 + 配置建议。
    ⚠️ 需 LLM key、耗 token。Redis 缓存 30min。"""
    def compute():
        from cache import cache_get
        import akshare as ak
        sina = cache_get("sector:sina")
        if not sina:
            df = ak.stock_sector_spot(indicator="新浪行业")
            sina = sorted([{"板块": r.get("板块"), "涨跌幅": _f(r.get("涨跌幅")),
                            "领涨股": r.get("股票名称")} for _, r in df.iterrows()],
                          key=lambda x: x.get("涨跌幅") or -1e9, reverse=True)
        ths = cache_get("sector:ths") or []
        top = sina[:10]
        bottom = sina[-8:]
        flow = sorted([s for s in ths if s.get("净流入") is not None],
                      key=lambda x: x["净流入"], reverse=True)[:8]
        lines = ["【今日行业涨幅榜TOP10】"]
        lines += [f"{s['板块']} {s['涨跌幅']:+.2f}% (领涨 {s.get('领涨股','')})" for s in top if s.get("涨跌幅") is not None]
        lines.append("\n【跌幅榜】")
        lines += [f"{s['板块']} {s['涨跌幅']:+.2f}%" for s in bottom if s.get("涨跌幅") is not None]
        if flow:
            lines.append("\n【资金净流入TOP8(同花顺)】")
            lines += [f"{s['板块']} 净流入 {s['净流入']/1e8:.2f}亿" for s in flow]
        user = "\n".join(lines) + "\n\n基于以上行业涨跌与资金流,简洁给出:① 资金轮入的主线板块(2-3个,带逻辑)② 轮出/走弱板块 ③ 当前市场风格判断 ④ 一句话配置建议。用数据说话,别空话。"
        r = _llm_analyze("你是A股板块轮动分析师,输出简洁中文要点(markdown)。", user, max_tokens=900)
        return r
    try:
        return _ok(_cache_or("sector_ai", 1800, compute, keep=lambda v: isinstance(v, dict) and v.get("analysis")))
    except Exception as e:
        return _err(e)


# ============================ AI 工作流(自定义编排) ============================
@app.get("/api/workflow/providers")
def workflow_providers():
    """可用数据块清单(key/名称/scope)。"""
    try:
        import ai_workflow
        return _ok(ai_workflow.list_providers())
    except Exception as e:
        return _err(e)


@app.get("/api/workflow/list")
def workflow_list():
    """工作流模板列表(首次自动灌种子)。"""
    try:
        import ai_workflow
        ai_workflow.ensure_seeds()
        return _ok(ai_workflow.list_workflows())
    except Exception as e:
        return _err(e)


class WorkflowSaveReq(BaseModel):
    id: int | None = None
    name: str
    scope: str = "stock"
    description: str = ""
    config: dict


@app.post("/api/workflow/save")
def workflow_save(req: WorkflowSaveReq):
    try:
        import ai_workflow
        rid = ai_workflow.save_workflow(req.id, req.name, req.scope, req.description, req.config)
        return _ok({"id": rid})
    except Exception as e:
        return _err(e)


@app.delete("/api/workflow/{wid}")
def workflow_delete(wid: int):
    try:
        import ai_workflow
        ai_workflow.delete_workflow(wid)
        return _ok({"deleted": wid})
    except Exception as e:
        return _err(e)


class WorkflowRunReq(BaseModel):
    config: dict
    params: dict = {}
    workflow_id: int | None = None
    name: str = ""


@app.get("/api/workflow/preview-data")
def workflow_preview_data(keys: str = "", code: str = "", query: str = ""):
    """预览所选数据块对当前参数返回什么(搭提示词时先看数据)。keys 逗号分隔。"""
    try:
        import ai_workflow
        ks = [k.strip() for k in (keys or "").split(",") if k.strip()]
        ctx = {"code": code, "query": query}
        return _ok(ai_workflow.fetch_data(ks, ctx))
    except Exception as e:
        return _err(e)


@app.get("/api/workflow/runs")
def workflow_runs(workflow_id: int = 0):
    """运行历史(元数据+final预览)。workflow_id=0 表示全部。"""
    try:
        import ai_workflow
        return _ok(ai_workflow.list_runs(workflow_id or None))
    except Exception as e:
        return _err(e)


@app.get("/api/workflow/run/{run_id}")
def workflow_run_detail(run_id: int):
    """单次运行完整结果。"""
    try:
        import ai_workflow
        return _ok(ai_workflow.get_run(run_id))
    except Exception as e:
        return _err(e)


@app.post("/api/workflow/run")
def workflow_run(req: WorkflowRunReq):
    """执行工作流(两层:并行分析→综合)。⚠️ 需 LLM key、耗 token。"""
    try:
        import ai_workflow
        result = ai_workflow.run_workflow(req.config, req.params)
        ai_workflow.save_run(req.workflow_id, req.name or req.config.get("name", ""), req.params, result)
        return _ok(result)
    except Exception as e:
        return _err(e)


# ============================ ☀️ 一键晨报 ============================
@app.get("/api/briefing/morning")
def briefing_morning(sell_n: int = 5, buy_n: int = 6, force: bool = False):
    """一键晨报:大盘速览 + 多因子买入推荐 + 持仓逐只信号(卖出提示+买卖点)。Redis 缓存 2h。
    逻辑在 core/briefing.py(webui/jobs/独立脚本共用)。"""
    try:
        import briefing
        if force:
            # 用户点「刷新」:重算一次(约1分钟,持仓扫描有50s上限),写文件缓存供全天读
            return _ok(briefing.cached_briefing(sell_n, buy_n, force=True))
        # 页面加载:只读缓存,有就秒回、没有给提示(绝不冷算,页面永不卡)
        out = briefing.cached_briefing(sell_n, buy_n, force=False)
        if out is None:
            out = {"market": {}, "buy": [], "sell": [], "hold_buy": [], "scanned": None, "_warming": True}
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.post("/api/briefing/ai-summary")
def briefing_ai_summary():
    """晨报 AI 一句话总结(可选,需 LLM key)。基于已缓存的晨报数据。"""
    try:
        from cache import cache_get
        data = cache_get("briefing:5:6") or {}
        if not data:
            data = briefing_morning().get("data", {})
        m = data.get("market", {})
        buy = ",".join(x["code"] for x in data.get("buy", []))
        sell = ",".join("%s(%s)" % (x["name"], "/".join(x["sell_reasons"])) for x in data.get("sell", []))
        user = (f"大盘:{m.get('indices')}\n强势板块:{m.get('sector_top')}\n"
                f"多因子买入候选:{buy}\n持仓建议关注卖出:{sell}\n"
                "请用3-4句话给出今日操作要点:大盘基调、是否加减仓、重点提示。简洁口语。")
        return _ok(_llm_analyze("你是私人投资助理,给懒人用户简洁的每日操作提示。", user, max_tokens=500))
    except Exception as e:
        return _err(e)


# ============================ 宏观 ============================
@app.get("/api/macro")
def macro_data():
    """宏观周期数据快照(GDP/CPI/PMI/货币/利率…)。冷抓 ~70s,但宏观指标按月/季更新 →
    缓存 12h(冷加载极少触发;生产可由后台任务预热)。"""
    try:
        data = _cache_or("macro:all", 43200,
                         lambda: MacroCycleDataFetcher_get(),
                         keep=lambda v: isinstance(v, dict) and bool(v))
        return _ok(data)
    except Exception as e:
        return _err(e)


def MacroCycleDataFetcher_get():
    from macro_cycle_data import MacroCycleDataFetcher
    return MacroCycleDataFetcher().get_all_macro_data()


# ============================ 语义检索(RAG) ============================
@app.get("/api/rag/search")
def rag_search(q: str, top_k: int = 8, sources: str = ""):
    """语义检索(BGE-M3→pgvector→TEI rerank)。sources 逗号分隔过滤 analysis/news/reco/report。"""
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rag"))
        import service
        st = [s for s in sources.split(",") if s] or None
        return _ok(service.semantic_search(q, top_k=top_k, source_types=st))
    except Exception as e:
        return _err(e)


# ============================ 妙想(东财 AI 第二意见) ============================
@app.get("/api/miaoxiang")
def miaoxiang_query(skill: str = "ask", q: str = ""):
    """妙想(东方财富 AI)外部第二意见。skill ∈ stock_diagnosis/ask/hotspot/comparable/
    finance_search/macro_data/industry_report/topic_report/fund_diagnosis。
    ⚠️ 问句会发往东财服务器(合规自评);未配 EM_API_KEY 用 demo key(易限流)。"""
    try:
        import miaoxiang
        if not (q or "").strip():
            return _err("请输入问题")
        return _ok(miaoxiang.query(skill, q))
    except Exception as e:
        return _err(e)


@app.get("/api/rag/stats")
def rag_stats():
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rag"))
        import store, embed_client
        return _ok({"store": store.stats(), "services": embed_client.health()})
    except Exception as e:
        return _err(e)


# ============================ 历史:AI推荐战绩 ============================
@app.get("/api/history/eval")
def history_eval(days: int = 90):
    """AI 推荐评估战绩(盈利反馈环,按 source 真实胜率/收益)。⚠️ 需 PG(ai_recommendations 表)。"""
    try:
        from ai_evaluation import evaluate_by_source, evaluate_all, format_report
        overall = evaluate_all(days=days)
        by = evaluate_by_source(days=days)
        return _ok({
            "overall": {"sample": overall.sample_size, "score": overall.score, "grade": overall.grade},
            "report": format_report(by),
        })
    except Exception as e:
        return _err(e)


@app.get("/api/history/eval/by")
def history_eval_by(dim: str = "source", days: int = 90):
    """AI 推荐胜率按维度分桶(source/confidence/horizon/outcome/month)。⚠️ 需 PG(ai_recommendations 表)。"""
    try:
        from ai_evaluation import evaluate_by, format_buckets, VALID_DIMENSIONS
        if dim not in VALID_DIMENSIONS:
            return _err(f"不支持的维度 {dim};可用:{', '.join(VALID_DIMENSIONS)}")
        results = evaluate_by(dim, days=days)
        buckets = [{
            "bucket": k, "sample": r.sample_size, "score": r.score, "grade": r.grade,
            "win_rate_pct": r.metrics.get("win_rate_pct"),
            "avg_return_pct": r.metrics.get("avg_return_pct"),
            "profit_factor": r.metrics.get("profit_factor"),
        } for k, r in results.items()]
        return _ok({"dimension": dim, "days": days,
                    "report": format_buckets(results, dim), "buckets": buckets})
    except Exception as e:
        return _err(e)


# ============================ 清仓决策助手 ============================
@app.get("/api/portfolio/exit-advice")
def portfolio_exit_advice(target: int = 10):
    """清仓决策助手:全持仓清仓紧迫分排序 + 过度分散瘦身建议 + AI 整体策略。回答"买太多何时清"。"""
    try:
        from exit_advisor import run_exit_advice
        return _ok(run_exit_advice(target_positions=target, record_signals=True))
    except Exception as e:
        return _err(e)


# ============================ LLM 用量遥测 ============================
@app.get("/api/llm/usage")
def llm_usage_summary(days: int = 30):
    """LLM Token 用量汇总:总量 / 按 model / 按 call_type / 按天 / 最近调用。"""
    try:
        import llm_usage
        return _ok(llm_usage.summary(days=days))
    except Exception as e:
        return _err(e)


# ============================ 研报:东财行业研报 ============================
@app.get("/api/research/industry-reports")
def research_industry_reports(industry_code: str = "*", pages: int = 5, begin: str = "2024-01-01"):
    """东财行业研报列表(走 datahub:industry_reports;industry_code='*' 取全行业)。"""
    try:
        import datahub
        rows = datahub.industry_reports(industry_code=industry_code, max_pages=pages, begin=begin)
        return _ok({"industry_code": industry_code, "count": len(rows or []), "reports": rows or []})
    except Exception as e:
        return _err(e)


# ============================ 决策信号(统一信号层)============================
@app.get("/api/signals")
def signals_list(code: str = "", status: str = "active", action: str = "",
                 source_type: str = "", days: int = 0, limit: int = 100):
    """决策信号列表(每次分析/选股/盯盘的结构化操作建议,带生命周期+去重)。
    status 传空=全部状态;days>0 限近 N 天。"""
    try:
        from decision_signal import list_signals
        return _ok(list_signals(code=code or None, status=status or None,
                                action=action or None, source_type=source_type or None,
                                days=days or None, limit=limit))
    except Exception as e:
        return _err(e)


@app.get("/api/signals/latest/{code}")
def signals_latest(code: str):
    """某股最新活跃决策信号(给持仓/盯盘联动用)。"""
    try:
        from decision_signal import get_latest_active
        return _ok(get_latest_active(code) or {})
    except Exception as e:
        return _err(e)


class SignalStatusReq(BaseModel):
    status: str   # closed / archived / invalidated / expired


@app.post("/api/signals/{signal_id}/status")
def signals_set_status(signal_id: int, req: SignalStatusReq):
    """手动改信号状态(closed/archived 等;终态不可逆回 active)。"""
    try:
        from decision_signal import update_status
        return _ok({"updated": update_status(signal_id, req.status)})
    except Exception as e:
        return _err(e)


@app.post("/api/signals/outcomes/run")
def signals_run_outcomes(days: int = 60, force: bool = False):
    """后验校验:对近 days 天、已过持有周期的信号用 K线判 hit/miss/neutral。"""
    try:
        from decision_signal import run_outcomes
        return _ok(run_outcomes(days=days, force=force))
    except Exception as e:
        return _err(e)


@app.get("/api/signals/outcomes/stats")
def signals_outcome_stats(dimension: str = "action", days: int = 180):
    """已评信号按维度(action/source_type/horizon)分桶胜率。"""
    try:
        from decision_signal import outcome_stats
        return _ok(outcome_stats(dimension=dimension, days=days))
    except Exception as e:
        return _err(e)


# ============================ 设置:定时任务开关 ============================
@app.get("/api/jobs")
def jobs_list():
    try:
        from automation_config import list_all
        return _ok(list_all())
    except Exception as e:
        return _err(e)


class ToggleReq(BaseModel):
    on: bool


@app.post("/api/jobs/{name}/toggle")
def jobs_toggle(name: str, req: ToggleReq):
    try:
        from automation_config import set_enabled
        ok = set_enabled(name, req.on, note="webui")
        return _ok({"name": name, "on": req.on, "saved": ok})
    except Exception as e:
        return _err(e)


@app.get("/api/jobs/{name}/runs")
def jobs_runs(name: str, limit: int = 8):
    try:
        from automation_config import get_recent_runs
        return _ok(get_recent_runs(name, limit))
    except Exception as e:
        return _err(e)


# 2026-06-12 任务整合后:部分 wf_* 已并入父任务,有独立私有函数的可手动触发该子流程
# (函数自带开关/交易日守卫,关或非交易日会自行跳过)。
_SUBSTEP_FNS = {
    "wf_daily_strategy_scan": "_daily_strategy_scan",
    "wf_daily_candidate_pool": "_daily_candidate_pool",
    "wf_position_profit_check": "_position_profit_check",
    "wf_position_guard_check": "_position_guard_check",
}
# 注册名 → 任务函数名(不等于 task_{name} 的别名)
_JOB_FN_ALIAS = {"wf_weekly_backtest": "task_weekly_backtest", **_SUBSTEP_FNS}
# 纯内联子步骤(无独立函数,只在父任务数据流内按本开关生效)→ 引导触发父任务
_JOB_INLINE_PARENT = {
    "wf_daily_pattern_alert": "portfolio_indicator_snapshot",
    "wf_overnight_to_rec": "morning_strategy",
    "wf_selection_to_rec": "unified_selection",
}


@app.post("/api/jobs/{name}/run")
def jobs_run(name: str):
    """手动立即触发一个定时任务(后台线程执行,HTTP 立即返回)。
    解决"只能等定时 / 走 MCP / SSH"的痛点。"""
    import threading
    try:
        from jobs import jobs_hub
        if name in _JOB_INLINE_PARENT:
            p = _JOB_INLINE_PARENT[name]
            return _err(f"「{name}」是已并入「{p}」的内联子步骤(随父任务按本开关运行),不能单独触发;请改触发父任务「{p}」")
        fn = getattr(jobs_hub, _JOB_FN_ALIAS.get(name, "task_" + name), None)
        if fn is None or not callable(fn):
            return _err(f"未知或不可手动触发的任务: {name}")
        threading.Thread(target=fn, name=f"manual-{name}", daemon=True).start()
        note = "（子步骤自带开关/交易日守卫:开关关闭或非交易日会自行跳过）" if name in _SUBSTEP_FNS else ""
        return _ok({"name": name, "triggered": True, "note": note})
    except Exception as e:
        return _err(e)


# ============================ 设置:环境配置(.env) ============================
@app.get("/api/env")
def env_get():
    """白名单环境配置(密钥脱敏,绝不回传明文)。"""
    try:
        import env_config
        return _ok(env_config.get_config())
    except Exception as e:
        return _err(e)


class EnvReq(BaseModel):
    updates: dict


@app.post("/api/env")
def env_set(req: EnvReq):
    """更新 .env(仅白名单键;secret 留空=保持不变)。改后需重启进程才对已加载模块完全生效。"""
    try:
        import env_config
        return _ok(env_config.update_config(req.updates or {}))
    except Exception as e:
        return _err(e)


@app.get("/api/convertible/screen")
def convertible_screen(top_n: int = 30, max_price: float = 135.0,
                       max_premium: float = 40.0, min_rating: str = "A+", refresh: bool = False):
    """可转债"双低"选债 + 市场概览。双低=现价+转股溢价率(越低越好)。Redis 缓存 30min。"""
    def compute():
        from convertible_bond import screen_double_low, market_summary
        rows = screen_double_low(top_n=top_n, max_price=max_price,
                                 max_premium=max_premium,
                                 min_rating=(min_rating or None))
        return {"summary": market_summary(), "picks": rows}
    try:
        key = f"cb_screen:{top_n}:{max_price}:{max_premium}:{min_rating}"
        out = compute() if refresh else _cache_or(key, 1800, compute,
                                                  keep=lambda v: bool(v and v.get("picks")))
        return _ok(out)
    except Exception as e:
        return _err(e)


@app.get("/api/screen/strategy/{name}")
def screen_strategy(name: str, top_n: int = 10):
    """问财策略选股(需 pywencai)。name: value/small_cap/profit_growth/low_price_bull/main_force。"""
    import pandas as pd
    try:
        if name == "value":
            from value_stock_selector import ValueStockSelector
            ok, df, msg = ValueStockSelector().get_value_stocks(top_n=top_n)
        elif name == "small_cap":
            from small_cap_selector import SmallCapSelector
            ok, df, msg = SmallCapSelector().get_small_cap_stocks(top_n=top_n)
        elif name == "profit_growth":
            from profit_growth_selector import ProfitGrowthSelector
            ok, df, msg = ProfitGrowthSelector().get_profit_growth_stocks(top_n=top_n)
        elif name == "low_price_bull":
            from low_price_bull_selector import LowPriceBullSelector
            ok, df, msg = LowPriceBullSelector().get_low_price_stocks(top_n=top_n)
        elif name == "main_force":
            from main_force_selector import MainForceStockSelector
            sel = MainForceStockSelector()
            raw = sel.get_main_force_stocks()
            df = raw if isinstance(raw, pd.DataFrame) else sel._convert_to_dataframe(raw)
            if df is not None and len(df):
                df = sel.get_top_stocks(df, top_n)
            ok, msg = (df is not None and len(df) > 0), "主力资金选股"
        else:
            return _err("未知策略")
        if not ok or df is None or len(df) == 0:
            return _ok({"rows": [], "msg": str(msg)})
        return _ok({"rows": df.where(pd.notna(df), None).to_dict("records"), "msg": str(msg)})
    except Exception as e:
        return _err(e)


@app.get("/api/strategy-genome/scores")
def strategy_genome_scores(days: int = 30):
    """策略基因组评分历史"""
    try:
        from core.database_pg import get_conn
        import psycopg2.extras
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT DISTINCT ON (strategy_id) strategy_id, score, win_rate_pct, avg_ret_pct,
                   stock_pool_n, triggered_n, max_dd_pct, best_ret_pct, worst_ret_pct,
                   eval_date, market_regime
            FROM strategy_scores
            WHERE eval_date >= (CURRENT_DATE - %s::integer)
            ORDER BY strategy_id, eval_date DESC
        """, (days,))
        rows = [dict(r) for r in cur.fetchall()]
        rows.sort(key=lambda x: x.get('score') or 0, reverse=True)
        cur.close()
        conn.close()
        return _ok({"rows": rows})
    except Exception as e:
        return _err(e)


@app.get("/api/strategy-genome/variants")
def strategy_genome_variants(strategy_id: str = None, limit: int = 50):
    """策略变体列表"""
    try:
        from analysis.strategy_genome import get_active_variants
        variants = get_active_variants(strategy_id=strategy_id, limit=limit)
        for v in variants:
            for dt_key in ('created_at', 'evaluated_at'):
                if v.get(dt_key):
                    v[dt_key] = v[dt_key].isoformat()
        return _ok({"rows": variants})
    except Exception as e:
        return _err(e)


@app.get("/api/strategy-genome/affinity")
def strategy_genome_affinity(stock_code: str):
    """个股策略适配度"""
    try:
        from core.database_pg import get_conn
        import psycopg2.extras
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM stock_strategy_affinity
            WHERE stock_code = %s AND last_eval_date >= (CURRENT_DATE - 60)
            ORDER BY score DESC
        """, (stock_code,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return _ok({"rows": rows})
    except Exception as e:
        return _err(e)


@app.get("/api/strategy-genome/scores/history")
def strategy_genome_scores_history(strategy_id: str, days: int = 30):
    """某策略评分历史趋势"""
    try:
        from core.database_pg import get_conn
        import psycopg2.extras
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT eval_date, score, win_rate_pct, avg_ret_pct, triggered_n, stock_pool_n
            FROM strategy_scores
            WHERE strategy_id = %s AND eval_date >= (CURRENT_DATE - %s::integer)
            ORDER BY eval_date ASC
        """, (strategy_id, days))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return _ok({"rows": rows})
    except Exception as e:
        return _err(e)


@app.get("/api/health")
def health():
    return _ok({"service": "shadow-foliant webui", "ok": True})


# 静态 SPA(放最后,catch-all 不影响上面的 /api 路由)
if os.path.isdir(_STATIC):
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")
