"""
妙想(AI-SaaS)可选顾问 —— 独立模块,挂了不影响任何主流程

设计原则:
  - 收盘综述复用当天的尾盘持仓快照和正式选股
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
    """保留完整妙想报告并通过统一渠道存档/投递。"""
    try:
        from notify.notification_router import send
        result = send('report', '妙想第二意见', text, only_channels=['qq'],
                      source='jobs.mx_advisor', compact=False)
        return bool(result.get('qq', (False, ''))[0])
    except Exception:
        print('[mx_advisor] ⚠️ Webhook 发送失败')
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


def run_daily_wrap(*, now=None) -> dict:
    """Use the dated closing decision and report missing inputs explicitly."""
    from zoneinfo import ZoneInfo
    from data.research_store import ResearchStore
    from jobs.jobs_hub import get_indicator_snapshot

    zone = ZoneInfo("Asia/Shanghai")
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    day = current.astimezone(zone).date().isoformat()
    closing = get_indicator_snapshot('_portfolio_close_review', day) or {}
    if closing.get('trade_date') != day or not closing.get('holdings'):
        return {'status': 'skipped', 'reason': 'same_day_close_review_missing', 'report': ''}

    formal = ResearchStore(ensure_schema=False).latest_formal_selection() or {}
    picks = []
    if str(formal.get('selection_date')) == day:
        picks = ((formal.get('artifacts') or {}).get('formal_top5') or {}).get('payload') or []

    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT total_daily_pnl, total_daily_pct, stock_count,
                                  fund_total_count, fund_fresh_count
                           FROM daily_pnl_snapshots WHERE snap_date = %s""", (day,))
            pnl = cur.fetchone()
    finally:
        conn.close()

    data_parts = []
    gaps = []
    coverage = closing.get('data_quality') or {}
    holdings = closing['holdings']
    data_parts.append(f"尾盘持仓 {len(holdings)} 只，行情覆盖 {(coverage.get('coverage') or 0):.0%}。")
    if closing.get('status') != 'success':
        data_parts.append('尾盘复核仅部分完成。')
        gaps.append('close_review_partial')
    for action_group, label in (
        ({'sell', 'reduce'}, '减仓或退出'), ({'add', 'buy'}, '买入或增持'), ({'hold'}, '持有'),
    ):
        matching = [row for row in holdings if row.get('action') in action_group]
        if matching:
            names = [f"{row.get('name') or row['symbol']}({row['symbol']})" for row in matching[:5]]
            data_parts.append(f"{label}共 {len(matching)} 只，示例：{'、'.join(names)}。")
    if picks:
        data_parts.append('今日正式 TOP5：' + '、'.join(str(row.get('code') or row.get('symbol')) for row in picks))
    else:
        gaps.append('same_day_top5_missing')
    if pnl is None or not pnl[2]:
        data_parts.append('今日收益快照待夜间汇总。')
        gaps.append('same_day_pnl_pending')
    else:
        value, pct, _, fund_total, fund_fresh = pnl
        if fund_total is None or (fund_fresh or 0) < fund_total:
            gaps.append('fund_pnl_partial')
        if value is not None and pct is not None:
            data_parts.append(f'已计入收益 {value:+,.0f} 元（{pct:+.2f}%），基金覆盖 {fund_fresh or 0}/{fund_total or 0}。')

    data_context = '\n'.join(data_parts)
    prompt = f"""根据 {day} 的尾盘持仓事实与正式选股做收盘复盘。

{data_context}

简述已确认的持仓风险、次日需要复核的事项和数据缺口。原样保留系统的持仓动作。
仅使用已给出的当日持仓事实；收益未出时不推算；正式候选只表示观察对象。"""

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
    lines = ['📰 **妙想收盘复盘**', '', *data_parts, '']
    if mx_content:
        lines.extend(['**模型复核参考**', mx_content])
    else:
        lines.append('妙想暂不可用。')

    lines.append('')
    lines.append('_数据来源：项目持仓分析 × 妙想AI-SaaS_')
    if not mx_content:
        gaps.append('model_unavailable')
    return {'status': 'partial' if gaps else 'success',
            'reason': ','.join(gaps) or 'complete', 'report': '\n'.join(lines)}


def mx_daily_wrap():
    """盘后可选任务:妙想每日综述"""
    try:
        report = run_daily_wrap()['report']
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
