"""
妙想(AI-SaaS)可选顾问 —— 独立模块,挂了不影响任何主流程

设计原则:
  - 完全独立,不 import 除 miaoxiang + psycopg2 以外的项目模块
  - 所有异常静默捕获,失败只 log 不 raise
  - 超时严格控制,单次调用 ≤ 90s,总任务 ≤ 5min
  - 仅对「卖出」+「高置信度」股票做第二意见,不泛滥调用

用法(直接 CLI):
  python -m jobs.mx_advisor           # 跑一次,拉取卖出Top5调妙想

用法(注册为 jobs_hub 任务):
  hub.register('mx_second_opinion', '08:35', mx_second_opinion)
"""

import os, sys, time, json
from datetime import datetime
from typing import Optional, Dict, List

# ── 路径引导 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 从根 .env 加载(不依赖项目的 dotenv init) ──
_ENV = {}
_env_path = os.path.join(_ROOT, '.env')
if os.path.isfile(_env_path):
    for line in open(_env_path, encoding='utf-8'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            _ENV[k.strip()] = v.strip().strip('"').strip("'")

# 注: EM_API_KEY 由 analysis/miaoxiang 自己读 os.getenv,这里先 set
if _ENV.get('EM_API_KEY'):
    os.environ.setdefault('EM_API_KEY', _ENV['EM_API_KEY'])

# ── 配置 ──
MAX_STOCKS = 5          # 最多调几只
API_TIMEOUT = 90         # 单次妙想调用超时(秒)
TOTAL_TIMEOUT = 300      # 整个任务总超时(秒)
DRY_RUN = os.getenv('MX_DRY_RUN', '').lower() in ('1', 'true', 'yes')


def _pg_conn():
    """获取 PG 连接(从 .env 读配置,不依赖项目全局 database_pg)"""
    import psycopg2
    return psycopg2.connect(
        host=_ENV.get('PG_HOST', '127.0.0.1'),
        port=int(_ENV.get('PG_PORT', '55432')),
        database=_ENV.get('PG_DATABASE', 'aiagents_stock'),
        user=_ENV.get('PG_USER', 'aiagents_stock'),
        password=_ENV.get('PG_PASSWORD', 'changeme'),
    )


def get_top_sell_stocks(limit: int = MAX_STOCKS) -> List[Dict]:
    """从 portfolio_analysis_history 取卖出评级 TopN(按置信度排序)"""
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (portfolio_stock_id) *
                FROM portfolio_analysis_history
                ORDER BY portfolio_stock_id, analysis_time DESC
            )
            SELECT p.code, p.name, h.rating, h.confidence, h.current_price
            FROM latest h
            JOIN portfolio_stocks p ON p.id = h.portfolio_stock_id
            WHERE h.rating = '卖出'
            ORDER BY h.confidence DESC, h.current_price DESC
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [
            {'code': r[0], 'name': r[1], 'rating': r[2],
             'confidence': float(r[3]) if r[3] else 0,
             'price': float(r[4]) if r[4] else 0}
            for r in rows
        ]
    except Exception as e:
        print(f'[mx_advisor] ⚠️ 查询持仓失败: {e}')
        return []


def call_miaoxiang_stock(code: str, name: str, timeout: int = API_TIMEOUT) -> Optional[str]:
    """调妙想 ask 接口,返回分析文本或 None"""
    if DRY_RUN:
        return f'[DRY_RUN] {code} {name} — 妙想分析(模拟)'
    try:
        from analysis.miaoxiang import query
        result = query('ask', f'分析{code} {name}的核心风险点和操作建议,200字以内', timeout=timeout)
        content = result.get('content', '')
        if content and content != '(无内容返回)':
            return content.strip()
        return None
    except Exception as e:
        print(f'[mx_advisor] ⚠️ {code} {name} 妙想调用失败: {e}')
        return None


def build_mx_report(results: List[Dict]) -> str:
    """生成妙想第二意见报告"""
    if not results:
        return ''
    lines = ['🐉 **妙想第二意见 — 卖出Top{}**'.format(len(results)), '']
    for i, r in enumerate(results, 1):
        code, name = r['code'], r['name']
        content = r.get('mx_content', '')
        if content:
            # 取前 200 字
            short = content[:200].replace('\n', ' ').strip()
            lines.append(f'{i}. **{name}**({code}) 置信{r["confidence"]:.0f} 现价{r["price"]}')
            lines.append(f'   > {short}')
        else:
            lines.append(f'{i}. {name}({code}) — 妙想暂无数据')
    lines.append('')
    lines.append('_妙想·东财AI-SaaS 第二意见,仅供参考_')
    return '\n'.join(lines)


def run_mx_advisor() -> Optional[str]:
    """
    执行一次妙想顾问:拉卖出Top5 → 逐只调妙想 → 返回报告文本
    返回 None 表示无可分析标的或全部失败
    """
    start = time.monotonic()
    print(f'[mx_advisor] 🧠 妙想顾问启动 (最多{MAX_STOCKS}只, 总超时{TOTAL_TIMEOUT}s)')

    # 1) 取 Top N
    stocks = get_top_sell_stocks(MAX_STOCKS)
    if not stocks:
        print('[mx_advisor] ⚠️ 无卖出标的,跳过')
        return None
    print(f'[mx_advisor] 待分析: {len(stocks)} 只')

    # 2) 逐只调用(串行,每只有超时)
    results = []
    for s in stocks:
        elapsed = time.monotonic() - start
        if elapsed > TOTAL_TIMEOUT:
            print(f'[mx_advisor] ⏰ 总超时({TOTAL_TIMEOUT}s),停止后续调用')
            break
        code, name = s['code'], s['name']
        remaining = max(API_TIMEOUT, TOTAL_TIMEOUT - elapsed)
        print(f'[mx_advisor] 调妙想 → {code} {name} (剩余{remaining:.0f}s)')
        mx = call_miaoxiang_stock(code, name, timeout=int(remaining))
        s['mx_content'] = mx
        results.append(s)

    # 3) 生成报告
    report = build_mx_report(results)
    elapsed = time.monotonic() - start
    print(f'[mx_advisor] ✅ 完成,耗时{elapsed:.1f}s,成功{sum(1 for r in results if r.get("mx_content"))}/{len(results)}')
    return report if report.strip() else None


def send_to_qq_webhook(text: str) -> bool:
    """通过 QQ Webhook 发送消息"""
    webhook_url = os.getenv('QQ_WEBHOOK_URL', 'http://127.0.0.1:18888/webhook/qq')
    try:
        import urllib.request as _req
        data = json.dumps({
            'msgtype': 'markdown',
            'markdown': {'text': text}
        }, ensure_ascii=False).encode('utf-8')
        req = _req.Request(webhook_url, data=data,
                          headers={'Content-Type': 'application/json'})
        _req.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f'[mx_advisor] ⚠️ Webhook 发送失败: {e}')
        return False


# ── jobs_hub 任务函数 ──
def mx_second_opinion():
    """盘后可选任务:对卖出Top5调妙想做第二意见,静默失败不影响主流程"""
    try:
        report = run_mx_advisor()
        if report:
            send_to_qq_webhook(report)
    except Exception as e:
        print(f'[mx_advisor] ⚠️ 任务整体失败(不影响主流程): {e}')


def _parse_kv(msg: str) -> dict:
    """解析 'ok=1 fail=52' / 'ok=1, fail=52; pool=99' 类字符串为字典"""
    d = {}
    # 先按分隔符拆成片段，再提取 k=v
    for sep in (';', ','):
        msg = msg.replace(sep, ' ')
    for token in msg.split():
        token = token.strip()
        if '=' in token:
            k, v = token.split('=', 1)
            d[k.strip()] = v.strip()
    return d


def _humanize_module(name: str, msg: str) -> str:
    """把模块的原始error转成一句话"""
    if not msg or msg.strip() == 'OK':
        return '一切正常'
    kv = _parse_kv(msg)

    if name == 'portfolio_indicator_snapshot':
        ok = int(kv.get('ok', 0))
        fail = int(kv.get('fail', 0))
        total = ok + fail
        if fail == 0:
            return f'{total}只全部获取成功 ✅'
        elif ok == 0:
            return f'{total}只全部获取失败 ❌ 数据源可能有问题'
        else:
            return f'{total}只中{ok}只成功、{fail}只失败'

    elif name == 'wf_portfolio_risk':
        total = int(kv.get('holdings', kv.get('total', 0)))
        alerts = int(kv.get('alerts', kv.get('alert', 0)))
        if alerts == 0:
            return f'{total}只持仓无风险警报 ✅'
        else:
            return f'{total}只持仓中有{alerts}只触发风险警报'

    elif name == 'wf_chan_scan':
        total = int(kv.get('holdings', kv.get('total', 0)))
        hits = int(kv.get('hits', kv.get('matched', 0)))
        if hits == 0:
            return f'{total}只均未出现缠论买卖点'
        else:
            return f'{total}只中发现{hits}个缠论信号'

    elif name == 'wf_daily_pattern_alert':
        total = int(kv.get('scanned', 0))
        pat = int(kv.get('pattern_alerts', 0))
        e_grade = int(kv.get('e_grade_alerts', 0))
        parts = []
        if pat:
            parts.append(f'发现{pat}只看跌形态')
        else:
            parts.append('无看跌形态')
        if e_grade:
            parts.append(f'{e_grade}只E等级基本面预警')
        return f'{total}只扫描完成，' + '，'.join(parts)

    elif name == 'wf_position_profit_check':
        triggered = int(kv.get('triggered', 0))
        critical = int(kv.get('critical', 0))
        if triggered == 0:
            return '无减仓信号'
        elif critical:
            return f'触发{triggered}次减仓信号，其中{critical}次严重❗'
        else:
            return f'触发{triggered}次减仓信号，均不严重'

    elif name == 'wf_daily_strategy_scan':
        pool = int(kv.get('pool', 0))
        matched = int(kv.get('matched', 0))
        inserted = int(kv.get('ai_inserted', 0))
        if matched == 0:
            return f'扫描{pool}只，无新策略匹配'
        else:
            return f'扫描{pool}只，匹配{matched}只，录入{inserted}只'

    elif name == 'morning_portfolio':
        buy = int(kv.get('buy', 0))
        sell = int(kv.get('sell', 0))
        return f'买入信号{buy}、卖出预警{sell}'

    elif name == 'afternoon_portfolio':
        buy = int(kv.get('buy', 0))
        sell = int(kv.get('sell', 0))
        return f'买入信号{buy}、卖出预警{sell}'

    elif name == 'unified_selection':
        picks = int(kv.get('picks', 0))
        return f'选出{picks}只候选'

    elif name == 'morning_strategy':
        candidates = int(kv.get('candidates', 0))
        return f'策略扫描产出{candidates}只候选'

    elif name == 'noon_report':
        return '午间市场简报已生成'

    elif name == 'afternoon_portfolio':
        buy = int(kv.get('buy', 0))
        sell = int(kv.get('sell', 0))
        return f'买入信号{buy}、卖出预警{sell}'

    elif name == 'daily_market_snapshot':
        return '大盘数据已更新'

    return _humanize_error(msg)


def _humanize_error(msg: str) -> str:
    """通用回退：把原始 error 字段转成通俗中文"""
    if not msg or msg.strip() == 'OK':
        return '一切正常'
    kv = _parse_kv(msg)
    tokens = []
    for k, v in kv.items():
        vi = v.lstrip('-')
        is_num = vi.isdigit()
        if k in ('success', 'success_count', 'ok', 'healthy'):
            tokens.append(f'成功{v}次' if is_num else f'成功{v}')
        elif k in ('failed', 'fail', 'error_count', 'alert', 'alerts'):
            tokens.append(f'失败{v}次' if is_num else f'失败{v}')
        elif k in ('total', 'count', 'stock_count', 'symbols', 'holdings'):
            tokens.append(f'共{v}只')
        elif k in ('scanned',):
            tokens.append(f'扫描{v}只')
        elif k in ('triggered', 'hits', 'matches', 'matched'):
            tokens.append(f'触发{v}次')
        elif k in ('critical', 'severe'):
            tokens.append(f'严重{v}个')
        elif k in ('warning', 'warn'):
            tokens.append(f'警告{v}个')
        elif k in ('na', 'no_data'):
            tokens.append(f'无数据{v}个')
        elif k in ('pool', 'universe'):
            tokens.append(f'股票池{v}只')
        elif k in ('picks', 'candidates', 'ai_inserted'):
            tokens.append(f'选中{v}只')
        else:
            tokens.append(f'{k}={v}')
    return ' '.join(tokens) if tokens else msg[:60]


def run_daily_wrap() -> Optional[str]:
    """妙想每日盘后综述:只提供项目独有持仓数据,市场数据由妙想自行查询"""
    start = time.monotonic()
    print(f'[mx_advisor] 📰 每日盘后综述启动')

    sell_items: list = []
    buy_items: list = []
    hold_items: list = []
    top_picks: list = []
    pnl_summary: str = ''
    afternoon_signal: str = ''

    try:
        conn = _pg_conn()
        cur = conn.cursor()

        # 1. 卖出Top5
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (portfolio_stock_id) * FROM portfolio_analysis_history
                ORDER BY portfolio_stock_id, analysis_time DESC
            )
            SELECT p.name, h.confidence, p.code
            FROM latest h JOIN portfolio_stocks p ON p.id = h.portfolio_stock_id
            WHERE h.rating = '卖出' ORDER BY h.confidence DESC LIMIT 5
        """)
        sell_items = [(r[0], int(r[1]), r[2]) for r in cur.fetchall() if r[0]]

        # 1b. 买入Top5
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (portfolio_stock_id) * FROM portfolio_analysis_history
                ORDER BY portfolio_stock_id, analysis_time DESC
            )
            SELECT p.name, h.confidence, p.code
            FROM latest h JOIN portfolio_stocks p ON p.id = h.portfolio_stock_id
            WHERE h.rating = '买入' ORDER BY h.confidence DESC LIMIT 5
        """)
        buy_items = [(r[0], int(r[1]), r[2]) for r in cur.fetchall() if r[0]]

        # 1c. 持有高置信度Top8
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (portfolio_stock_id) * FROM portfolio_analysis_history
                ORDER BY portfolio_stock_id, analysis_time DESC
            )
            SELECT p.name, h.confidence, p.code
            FROM latest h JOIN portfolio_stocks p ON p.id = h.portfolio_stock_id
            WHERE h.rating = '持有' ORDER BY h.confidence DESC LIMIT 8
        """)
        hold_items = [(r[0], int(r[1]), r[2]) for r in cur.fetchall() if r[0]]

        # 2. 今日选股(从 unified_selection 缓存)
        try:
            cur.execute("""
                SELECT data->>'picks' FROM indicator_snapshots
                WHERE indicator_key = '_last_selection'
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row and row[0]:
                picks = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                top_picks = picks[:10] if isinstance(picks, list) else []
        except Exception:
            pass

        # 3. 当日盈亏
        try:
            cur.execute("""
                SELECT total_value, daily_pnl, daily_pnl_pct FROM daily_pnl_snapshots
                WHERE snapshot_date = current_date ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                val, pnl, pnl_pct = row
                sign = '📈' if (pnl or 0) >= 0 else '📉'
                pnl_summary = f'{sign} 总市值 {val:,.0f} 日盈亏 {pnl:+,.0f}({pnl_pct:+.2f}%)' if val else ''
        except Exception:
            pass

        # 4. 尾盘持仓信号
        try:
            cur.execute("""
                SELECT error FROM job_runs
                WHERE job_name = 'afternoon_portfolio' AND started_at >= current_date
                ORDER BY started_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row and row[0]:
                kv = dict(x.split('=') for x in row[0].split() if '=' in x)
                buy = int(kv.get('buy', 0))
                sell = int(kv.get('sell', 0))
                afternoon_signal = f'买入信号{buy}、卖出预警{sell}'
        except Exception:
            pass

        # 基金净值(只打日志)
        try:
            cur.execute("""
                SELECT f.name, n.daily_return FROM fund_nav n
                JOIN funds f ON f.code = n.fund_code
                WHERE n.nav_date = current_date AND n.daily_return IS NOT NULL
                ORDER BY n.daily_return LIMIT 20
            """)
            fund_rows = cur.fetchall()
            if fund_rows:
                changes = ', '.join(f'{n}({dr:+.2f}%)' for n, dr in fund_rows)
                print(f'[mx_advisor] 💵 基金: {changes}')
        except Exception:
            pass

        cur.close()
        conn.close()
    except Exception as e:
        print(f'[mx_advisor] ⚠️ 数据收集失败: {e}')
        return None

    # 至少有一项数据才输出
    if not sell_items and not buy_items and not hold_items and not top_picks and not pnl_summary and not afternoon_signal:
        print('[mx_advisor] ⚠️ 无今日数据,跳过')
        return None

    # 组装数据摘要(给妙想的上下文)
    data_parts = []
    if pnl_summary:
        data_parts.append(f'持仓盈亏：{pnl_summary}')
    if sell_items:
        names = [f'{n}({c})' for n, c, _ in sell_items[:5]]
        data_parts.append(f'📉 卖出预警Top5：{", ".join(names)}')
    if buy_items:
        names = [f'{n}({c})' for n, c, _ in buy_items]
        data_parts.append(f'📈 买入信号：{", ".join(names)}')
    if hold_items:
        names = [f'{n}({c})' for n, c, _ in hold_items[:8]]
        data_parts.append(f'📊 持有(高置信)：{", ".join(names)}')
    if afternoon_signal:
        data_parts.append(f'尾盘持仓信号：{afternoon_signal}')
    if top_picks:
        data_parts.append(f'今日综合选股Top10：{", ".join(top_picks[:10])}')

    data_context = '\n'.join(data_parts)

    # 妙想提示词：交易员复盘风格，覆盖持仓全貌（卖/买/持 + 选股）
    prompt = f"""你是A股交易员，现在是交易日收盘后，请根据以下持仓数据做收盘复盘。

【今日持仓全貌】
{data_context}

请按以下三部分输出（简洁、直接，不要开场白）：

1. **持仓诊断**：卖出预警哪些需要重视？买入信号是否值得跟进？持有标的有无变盘风险？
2. **操作建议**：明天开盘后哪些该减仓/清仓，哪些可以继续持有或加仓？对照综合选股结果给具体建议。
3. **明日方向**：基于持仓结构判断明天应该进攻还是防守，给出1-2个具体关注方向。

注意：
- 市场整体行情你自己查询，不要让我提供
- 不要客套话，直接讲判断和建议
- 每个部分2-4句话即可"""

    # 调妙想
    mx_content = ''
    try:
        from analysis.miaoxiang import query
        result = query('ask', prompt, timeout=120)
        mx_content = result.get('content', '')
        if mx_content in ('(无内容返回)', '', None):
            mx_content = ''
        else:
            mx_content = mx_content.strip()
            # 去掉可能的开场白
            for prefix in ('好的', '收到', '明白了', '根据您', '以下是', '为您'):
                if mx_content.startswith(prefix):
                    idx = mx_content.find('\n')
                    if idx > 0:
                        mx_content = mx_content[idx+1:].strip()
                    break
    except Exception as e:
        print(f'[mx_advisor] ⚠️ 妙想调用失败: {e}')

    # 组装最终报告
    lines = ['📰 **妙想收盘复盘**', '']
    if mx_content:
        lines.append(mx_content)
    else:
        lines.append('_(妙想暂不可用，以下为持仓数据摘要)_')
        lines.append('')
        lines.extend(data_parts)

    lines.append('')
    lines.append('_数据来源：项目持仓分析 × 妙想AI-SaaS_')
    report = '\n'.join(lines)
    elapsed = time.monotonic() - start
    print(f'[mx_advisor] 📰 综述完成,耗时{elapsed:.1f}s')
    return report


def mx_daily_wrap():
    """盘后可选任务:妙想每日综述"""
    try:
        report = run_daily_wrap()
        if report:
            send_to_qq_webhook(report)
    except Exception as e:
        print(f'[mx_advisor] ⚠️ 综述失败(不影响主流程): {e}')


# ── CLI ──
if __name__ == '__main__':
    report = run_mx_advisor()
    if report:
        print('\n' + report)
        send_to_qq_webhook(report)
    else:
        print('[mx_advisor] 无结果')
