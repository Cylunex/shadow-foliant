"""
持仓数据洞察 — 报表 + AI 诊断

提供 4 类报表 + 1 个 AI 诊断 Agent：
  1. portfolio_valuation       — 当前持仓估值（用最新价 vs 成本价）
  2. portfolio_change_timeline — 持仓变动时间线 + 统计
  3. holding_duration_distribution — 持有时长分布
  4. trading_frequency_analysis — 交易频次、最活跃股票

  diagnose_portfolio(model=None) — DeepSeek AI 综合诊断 + 给出改进建议
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _get_current_price(symbol: str) -> Optional[float]:
    """获取当前价（用 a-stock HTTP，最快）"""
    try:
        import datahub
        quote = datahub.quote(symbol)
        if quote and quote.get('price'):
            return float(quote['price'])
    except Exception:
        pass
    return None


# =============================================================================
# 报表 1: 持仓估值
# =============================================================================
def portfolio_valuation(with_current_price: bool = True) -> Dict:
    """计算当前持仓估值

    Returns:
        {
          'total_cost': 总成本,
          'total_value': 当前市值（若可拉到价格）,
          'total_pnl': 浮动盈亏,
          'total_pnl_pct': 整体收益率,
          'stocks': [{code, name, qty, cost_price, current_price, value, pnl, pnl_pct}, ...]
        }
    """
    from portfolio_db import portfolio_db
    stocks = portfolio_db.get_all_stocks() or []
    total_cost = 0.0
    total_value = 0.0
    rows = []
    # 批量取价(一次 datahub.quotes 代替逐只 datahub.quote)→ 避免 N 次串行报价
    price_map = {}
    if with_current_price and stocks:
        try:
            import datahub
            codes = [str(s.get('code')) for s in stocks if s.get('code')]
            q = datahub.quotes(codes) or {}
            for c, v in q.items():
                p = (v or {}).get('price')
                if p:
                    price_map[str(c)] = float(p)
        except Exception:
            pass
    for s in stocks:
        cost_price = s.get('cost_price') or 0
        qty = s.get('quantity') or 0
        # PG decimal -> float
        try:
            cost_price = float(cost_price)
        except (TypeError, ValueError):
            cost_price = 0.0
        cost = cost_price * qty
        total_cost += cost
        current_price = price_map.get(str(s.get('code'))) if with_current_price else None
        value = (current_price or cost_price) * qty
        total_value += value
        pnl = value - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        rows.append({
            'code': s['code'],
            'name': s.get('name', ''),
            'quantity': qty,
            'cost_price': cost_price,
            'current_price': current_price,
            'cost': cost,
            'value': value,
            'pnl': pnl,
            'pnl_pct': round(pnl_pct, 2),
        })
    rows.sort(key=lambda r: -(r['pnl'] or 0))
    return {
        'total_cost': round(total_cost, 2),
        'total_value': round(total_value, 2),
        'total_pnl': round(total_value - total_cost, 2),
        'total_pnl_pct': round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        'stock_count': len(rows),
        'stocks': rows,
    }


# =============================================================================
# 报表 2: 变动时间线
# =============================================================================
def portfolio_change_timeline(since_days: int = 90, limit: int = 100) -> Dict:
    """最近 N 天的所有持仓变动"""
    from portfolio_db import portfolio_db
    history = portfolio_db.get_change_history(since_days=since_days, limit=limit)
    stats = portfolio_db.get_change_stats(since_days=since_days)
    # 按日期分组
    by_day = {}
    for h in history:
        day = h['changed_at'].strftime('%Y-%m-%d') if isinstance(h['changed_at'], datetime) else str(h['changed_at'])[:10]
        by_day.setdefault(day, []).append(h)
    return {
        'since_days': since_days,
        'total_changes': len(history),
        'by_type': stats.get('by_type', {}),
        'most_active': stats.get('most_active', []),
        'by_day': dict(sorted(by_day.items(), reverse=True)),
        'recent_changes': history[:30],
    }


# =============================================================================
# 报表 3: 持有时长分布
# =============================================================================
def holding_duration_distribution() -> Dict:
    """每只持仓股票的持有时长（从首次 add 到现在）"""
    from portfolio_db import portfolio_db
    stocks = portfolio_db.get_all_stocks() or []
    rows = []
    now = datetime.now()
    for s in stocks:
        created = s.get('created_at')
        if created:
            try:
                if isinstance(created, str):
                    created = datetime.fromisoformat(created.replace('Z', '+00:00'))
                # PG TIMESTAMPTZ → datetime（带 tz），统一去 tz
                if hasattr(created, 'tzinfo') and created.tzinfo:
                    created = created.replace(tzinfo=None)
                days = (now - created).days
            except Exception:
                days = None
        else:
            days = None
        rows.append({
            'code': s['code'],
            'name': s.get('name', ''),
            'days_held': days,
            'quantity': s.get('quantity'),
        })
    # 分布桶
    buckets = {'<7d': 0, '7-30d': 0, '30-90d': 0, '90-180d': 0, '>180d': 0, 'unknown': 0}
    for r in rows:
        d = r['days_held']
        if d is None:
            buckets['unknown'] += 1
        elif d < 7:
            buckets['<7d'] += 1
        elif d < 30:
            buckets['7-30d'] += 1
        elif d < 90:
            buckets['30-90d'] += 1
        elif d < 180:
            buckets['90-180d'] += 1
        else:
            buckets['>180d'] += 1
    return {
        'buckets': buckets,
        'stocks': sorted(rows, key=lambda r: -(r['days_held'] or 0)),
        'avg_days': round(sum(r['days_held'] for r in rows if r['days_held']) /
                          max(1, sum(1 for r in rows if r['days_held'])), 1),
    }


# =============================================================================
# 报表 4: 交易频次分析
# =============================================================================
def trading_frequency_analysis(since_days: int = 90) -> Dict:
    """交易频次 / 换手率 / 活跃股票"""
    from portfolio_db import portfolio_db
    history = portfolio_db.get_change_history(since_days=since_days, limit=10000)
    # 统计买入次数和卖出次数
    buys = sum(1 for h in history if (h.get('delta_qty') or 0) > 0)
    sells = sum(1 for h in history if (h.get('delta_qty') or 0) < 0)
    bulk = sum(1 for h in history if h.get('source') in ('bulk_import',))
    # 每天平均交易数
    days = since_days or 1
    daily_avg = round(len(history) / days, 2)
    return {
        'since_days': since_days,
        'total_changes': len(history),
        'buys': buys,
        'sells': sells,
        'bulk_imports': bulk,
        'buy_sell_ratio': round(buys / max(1, sells), 2),
        'daily_avg_changes': daily_avg,
    }


# =============================================================================
# AI 持仓诊断 Agent
# =============================================================================
def diagnose_portfolio(model: Optional[str] = None) -> Dict:
    """调用 DeepSeek AI 综合诊断持仓 + 给改进建议

    输入：估值/变动/持有时长/交易频次 4 个报表
    输出：{summary, problems, suggestions, raw_analysis (截断)}
    """
    # 拉所有报表数据
    valuation = portfolio_valuation(with_current_price=True)
    timeline = portfolio_change_timeline(since_days=90)
    duration = holding_duration_distribution()
    frequency = trading_frequency_analysis(since_days=90)

    # 构造给 AI 的精简 context
    context = {
        'overview': {
            'stock_count': valuation['stock_count'],
            'total_cost': valuation['total_cost'],
            'total_value': valuation['total_value'],
            'total_pnl': valuation['total_pnl'],
            'total_pnl_pct': valuation['total_pnl_pct'],
        },
        'top_winners': [r for r in valuation['stocks'][:5] if (r.get('pnl') or 0) > 0],
        'top_losers': [r for r in reversed(valuation['stocks'][-5:]) if (r.get('pnl') or 0) < 0],
        'change_stats': {
            'last_90_days_total': timeline['total_changes'],
            'by_type': timeline['by_type'],
            'most_active': timeline['most_active'][:5],
        },
        'holding_duration': duration['buckets'],
        'avg_holding_days': duration['avg_days'],
        'trading_frequency': frequency,
    }

    prompt = f"""你是一名资深投资顾问。请基于以下用户的实盘持仓数据，从交易习惯、风险管理、收益结构 3 个维度做诊断。

【当前持仓总览】
- 股票数：{context['overview']['stock_count']} 只
- 总成本：{context['overview']['total_cost']:.2f} 元
- 当前市值：{context['overview']['total_value']:.2f} 元
- 浮动盈亏：{context['overview']['total_pnl']:.2f} 元（{context['overview']['total_pnl_pct']:.2f}%）

【盈利股票 TOP 5】
{chr(10).join(f"  - {s['code']} {s['name']}: 数量{s.get('quantity')} 成本{s.get('cost_price')} 现价{s.get('current_price')} 盈亏 {s.get('pnl_pct')}%" for s in context['top_winners']) or '  无'}

【亏损股票 TOP 5】
{chr(10).join(f"  - {s['code']} {s['name']}: 数量{s.get('quantity')} 成本{s.get('cost_price')} 现价{s.get('current_price')} 盈亏 {s.get('pnl_pct')}%" for s in context['top_losers']) or '  无'}

【交易行为】
- 最近 90 天变动数：{frequency['total_changes']} 次
- 买入次数 {frequency['buys']} / 卖出次数 {frequency['sells']}（买卖比 {frequency['buy_sell_ratio']}）
- 日均变动：{frequency['daily_avg_changes']} 次
- 持有时长分布：{duration['buckets']}
- 平均持有天数：{duration['avg_days']} 天

【最活跃股票（变动次数最多）】
{chr(10).join(f"  - {s['code']} {s['name']}: {s['count']} 次" for s in context['change_stats']['most_active']) or '  无'}

请以 JSON 格式输出分析结果（不要有额外说明文字）：
{{
    "summary": "1-2 句总体评价",
    "problems": [
        "问题1 — 具体到现象 + 数据",
        "问题2",
        "..."
    ],
    "suggestions": [
        "建议1 — 具体可操作",
        "建议2",
        "..."
    ],
    "risk_score": 1-10（1=极保守，10=极激进）,
    "discipline_score": 1-10（1=随性，10=有纪律）
}}
"""

    try:
        from deepseek_client import DeepSeekClient
        client = DeepSeekClient(model=model)
        messages = [
            {'role': 'system', 'content': '你是一名资深投资顾问，擅长从交易数据洞察投资者的习惯和盲点。'},
            {'role': 'user', 'content': prompt},
        ]
        raw = client.call_api(messages, max_tokens=2000)
        # 解析 JSON
        import json, re
        match = re.search(r'\{[\s\S]*\}', raw or '')
        if match:
            try:
                parsed = json.loads(match.group())
            except Exception:
                parsed = {'raw_text': (raw or '')[:1500]}
        else:
            parsed = {'raw_text': (raw or '')[:1500]}
    except Exception as e:
        parsed = {'error': str(e), 'note': '若 AI 调用失败，可查看 context 字段做人工分析'}

    return {
        'context': context,
        'diagnosis': parsed,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


# =============================================================================
# CLI 自检
# =============================================================================
if __name__ == '__main__':
    import json
    print('=== portfolio_valuation ===')
    v = portfolio_valuation(with_current_price=False)
    print(f"  股票 {v['stock_count']} 只  总成本 {v['total_cost']}")
    print()
    print('=== portfolio_change_timeline (90d) ===')
    t = portfolio_change_timeline(90)
    print(f"  变动 {t['total_changes']} 次  by_type={t['by_type']}")
    print()
    print('=== holding_duration_distribution ===')
    d = holding_duration_distribution()
    print(f"  桶: {d['buckets']}  avg={d['avg_days']}d")
    print()
    print('=== trading_frequency_analysis ===')
    f = trading_frequency_analysis(90)
    print(f"  90d: {f}")
