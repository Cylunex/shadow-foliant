#!/usr/bin/env python3
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""
每日定时分析脚本 — 龙虎榜 / 智能选股 / 持仓分析

模式:
  dragon_tiger    — 龙虎榜报告（盘前发送）
  morning_picks   — 早盘10只精选（开盘后发送）
  afternoon_picks — 尾盘持仓+选股（收盘前发送）
  noon_report     — 午盘市场简报（中午发送）

用法:
  python3 scripts/daily_signal_scan.py dragon_tiger
  python3 scripts/daily_signal_scan.py morning_picks
  python3 scripts/daily_signal_scan.py afternoon_picks
  python3 scripts/daily_signal_scan.py noon_report
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

# 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytz
import numpy as np
import pandas as pd
from io import StringIO

import datahub  # 统一数据层:行情/龙虎/板块/估值/资金流/强势股一律走 datahub(熔断+超时+多源兜底)
from a_stock_data_adapter import _eastmoney_datacenter  # 龙虎榜 RPT 明细,datahub 暂无对应,保留私有源

# ═══════════════════════════════════════════════════════════
#  通知层 — QQ Webhook 优先，邮件兜底
# ═══════════════════════════════════════════════════════════

# QQ Webhook(生产机本地代理;其他机器在 .env 配 QQ_WEBHOOK_URL,如 NAS nginx 入口)
import os as _os
QQ_WEBHOOK_URL = _os.getenv("QQ_WEBHOOK_URL", "").strip() or "http://127.0.0.1:18888/webhook/qq"

# 邮件兜底(从 env 读,勿硬编码密钥)
EMAIL_ENABLED = _os.getenv("EMAIL_ENABLED", "false").lower() in ("1", "true", "yes", "on")
SMTP_SERVER = _os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(_os.getenv("SMTP_PORT", "587"))
EMAIL_FROM = _os.getenv("EMAIL_FROM", "")
EMAIL_PASSWORD = _os.getenv("EMAIL_PASSWORD", "")
EMAIL_TO = _os.getenv("EMAIL_TO", "")


def send_via_qq_webhook(text: str, title: str = "") -> bool:
    """通过 QQ Webhook 发送（钉钉 markdown 格式 → webhook-to-qq 自动识别为 markdown 通道）"""
    try:
        import requests
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title or "股票分析报告",
                "text": text,
            }
        }
        r = requests.post(QQ_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"[QQ] ✅ 消息发送成功 ({len(text)} chars)")
            return True
        else:
            print(f"[QQ] ❌ 发送失败: {r.status_code}")
            return False
    except Exception as e:
        print(f"[QQ] ❌ 发送异常: {e}")
        return False


def send_via_email(subject: str, body: str) -> bool:
    """通过 QQ 邮箱发送"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"[Email] ✅ 邮件发送成功")
        return True
    except Exception as e:
        print(f"[Email] ❌ 发送失败: {e}")
        return False


def send_report(text: str, title: str = ""):
    """发送报告:优先走 notification_router 统一路由(report→默认QQ,全挂自动兜底邮件);
    路由不可用时退回本脚本自带的 QQ→邮件链(保持独立可运行)。"""
    # 截断长文本
    if len(text) > 15000:
        text = text[:14800] + "\n\n... (内容过长已截断)"

    try:
        from notification_router import send as _nr_send
        res = _nr_send('report', title or "股票分析报告", text)
        if any(ok for ok, _ in res.values()):
            print(f"[通知] ✅ 路由发送成功: {[c for c, (ok, _) in res.items() if ok]}")
            return
        print(f"[通知] 路由全部失败({res}),退回本地发送链")
    except Exception as e:
        print(f"[通知] 路由不可用({e}),退回本地发送链")

    # 本地兜底链: QQ Webhook → 邮件
    if send_via_qq_webhook(text, title):
        return
    print("[通知] QQ发送失败，尝试邮件兜底...")
    send_via_email(title or "股票分析报告", text)


# 持仓池 = 从数据库动态读取（非选股用）
# 实时从 portfolio_stocks.db 获取，不再硬编码

def get_portfolio_codes() -> list:
    """从项目持仓数据库获取股票代码列表"""
    try:
        from portfolio_manager import PortfolioManager
        pm = PortfolioManager()
        stocks = pm.get_all_stocks()
        codes = [s.get('code', '') for s in stocks if s.get('code')]
        print(f"[持仓] 从数据库加载 {len(codes)} 只股票")
        return codes
    except Exception as e:
        print(f"[持仓] 数据库加载失败: {e}，使用默认池")
        return [
            "600519", "000858", "601318", "600036", "000001",
            "002415", "300750", "002594", "000333", "600900",
        ]

# 全市场候选池（用于选股，排除持仓股）
# 覆盖各行业大中盘，约 60 只
MARKET_CANDIDATE_POOL = [
    # 金融
    "600016", "601398", "601939", "601288", "601328", "600919",
    # 消费
    "600887", "600809", "000568", "000895", "002714", "300146",
    # 科技/半导体
    "688981", "002230", "300274", "603259", "688012", "002049",
    # 新能源/光伏
    "600438", "601899", "002129", "300763",
    # 医药
    "300760", "000538", "300015", "002001",
    # 周期/基建
    "601668", "600048", "601088", "600028", "601857", "000002",
    # 制造业
    "600031", "002142", "600809", "601390", "600585",
    # 通信/计算机
    "600050", "000938", "300033", "002236",
    # 军工/航工
    "600893", "000768", "600760",
    # 其他
    "600690", "000100", "600104", "002352", "601006", "601919",
]


def is_trading_day(date=None):
    """简单判断是否为交易日（周末=False）"""
    d = date or datetime.now()
    return d.weekday() < 5  # Mon=0, Sun=6


def get_trading_date():
    """获取最近已完成的交易日（已有收盘数据的）"""
    now = datetime.now()
    hour, minute = now.hour, now.minute
    # datetime.now() 返回 CST 本地时间，交易时间 9:30-15:00 CST
    in_trading_hours = (hour > 9 or (hour == 9 and minute >= 30)) and hour < 15
    if in_trading_hours and now.weekday() < 5:
        return now.strftime("%Y-%m-%d")
    # 盘前（09:30前）或盘后（15:00后）或非交易日：返回上一个交易日（不含今天）
    for delta in range(1, 10):
        d = now - timedelta(days=delta)
        if d.weekday() < 5:
            return d.strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════
#  龙虎榜报告
# ═══════════════════════════════════════════════════════════

def dragon_tiger_report():
    """盘前龙虎榜报告"""
    print("🐉 生成龙虎榜报告...")
    trade_date = get_trading_date()

    lines = [
        f"🐉 龙虎榜日报",
        f"交易日: {trade_date}",
        "",
    ]

    # 1. 全市场龙虎榜
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=f"(TRADE_DATE>='{trade_date}')(TRADE_DATE<='{trade_date}')",
            page_size=200,
            sort_columns="BILLBOARD_NET_AMT", sort_types="-1",
        )
        if data:
            lines.append(f"📊 全市场龙虎榜 ({len(data)}条)")
            lines.append("───────────────")

            # TOP 净买入
            lines.append("  ")
            lines.append("🏆 净买入 TOP 10")
            for i, row in enumerate(data[:10], 1):
                code = row.get("SECURITY_CODE", "")
                name = row.get("SECURITY_NAME_ABBR", "")
                net = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
                buy = (row.get("BILLBOARD_BUY_AMT") or 0) / 10000
                sell = (row.get("BILLBOARD_SELL_AMT") or 0) / 10000
                reason = row.get("EXPLANATION", "")[:20]
                chg = round(float(row.get("CHANGE_RATE") or 0), 2)
                emoji = "🟢" if net > 5000 else ("🔴" if net < -5000 else "⚪")
                lines.append(
                    f"  {i}. {code} {name} {emoji}净买{net:.0f}万 "
                    f"买{buy:.0f}万卖{sell:.0f}万 涨{chg}%"
                )
                lines.append(f"     {reason}")

            # 净卖出 TOP 5
            sorted_sell = sorted(data, key=lambda x: (x.get("BILLBOARD_NET_AMT") or 0))
            lines.append("  ")
            lines.append("📉 净卖出 TOP 5")
            for i, row in enumerate(sorted_sell[:5], 1):
                code = row.get("SECURITY_CODE", "")
                name = row.get("SECURITY_NAME_ABBR", "")
                net = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
                lines.append(
                    f"  {i}. {code} {name} 🔴净买{net:.0f}万"
                )

            # 龙虎榜个股机构情况
            lines.append("  ")
            lines.append("🏦 机构动向摘要")
            inst_count = 0
            for row in data[:30]:
                code = row.get("SECURITY_CODE", "")
                name = row.get("SECURITY_NAME_ABBR", "")
                net = (row.get("BILLBOARD_NET_AMT") or 0) / 10000
                if abs(net) > 3000:
                    try:
                        dtb = datahub.dragon_tiger_stock(code, trade_date, 30)
                        inst = dtb.get("institution", {})
                        if inst.get("net_amt", 0) != 0:
                            inst_net = inst["net_amt"]
                            inst_count += 1
                            if inst_count <= 10:
                                sig = "🟢" if inst_net > 0 else "🔴"
                                lines.append(
                                    f"  {code} {name} 机构净买{sig}{inst_net}万 "
                                    f"(买{inst.get('buy_amt',0)}万 卖{inst.get('sell_amt',0)}万)"
                                )
                    except:
                        pass

            if inst_count == 0:
                lines.append("  今日龙虎榜无明显机构席位动向")
        else:
            lines.append("⚠️ 今日暂无龙虎榜数据（可能非交易日）")
    except Exception as e:
        lines.append(f"⚠️ 龙虎榜数据获取失败: {e}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  智能选股（早盘 / 尾盘通用）
# ═══════════════════════════════════════════════════════════

def smart_stock_picks(session_name="早盘", top_n=10):
    """智能选股：多维度筛选"""
    print(f"🔍 生成{ session_name }选股...")
    trade_date = get_trading_date()

    lines = [
        f"🔍 { session_name }智能选股",
        f"日期: {trade_date}",
        "",
    ]

    candidates = set()

    # ─── 维度1: 腾讯行情批量筛选（PE/PB/涨幅） ───
    try:
        lines.append("📊 基于实时行情筛选")

        # 尝试覆盖更多股票
        wide_watchlist = MARKET_CANDIDATE_POOL.copy()  # 非持仓候选池
        extra_codes = [
            "601899", "600028", "600887", "601088", "600809",
            "002142", "000002", "600048", "601668", "601857",
            "300274", "603259", "688981", "600438", "002230",
            "000938", "600050", "601398", "601939", "600016",
        ]
        for c in extra_codes:
            if c not in wide_watchlist:
                wide_watchlist.append(c)

        quotes = datahub.quotes(wide_watchlist)
        if quotes:
            # 估值筛选: PE 0-40, 换手率>0.3%
            pe_filtered = []
            for code, q in quotes.items():
                pe = q.get('pe_ttm', 0) or 0
                to = q.get('turnover_pct', 0) or 0
                chg = q.get('change_pct', 0) or 0
                if 0 < pe <= 40 and to > 0.3:
                    candidates.add(code)
                    pe_filtered.append((code, q))

            # 按换手率排序取TOP
            pe_filtered.sort(key=lambda x: x[1].get('turnover_pct', 0), reverse=True)
            for code, q in pe_filtered[:15]:
                pe = q.get('pe_ttm', 0) or 0
                pb = q.get('pb', 0) or 0
                chg = q.get('change_pct', 0) or 0
                to = q.get('turnover_pct', 0) or 0
                mcap = q.get('mcap_yi', 0) or 0
                lines.append(
                    f"  {code} {q.get('name','?'):8s} "
                    f"价{q.get('price',0):.1f} PE={pe:.1f} PB={pb:.1f} "
                    f"涨{chg:.1f}% 换手{to:.1f}% 市值{mcap:.0f}亿"
                )
    except Exception as e:
        lines.append(f"  ⚠️ 实时筛选失败: {e}")

    # ─── 维度2: 行业排行（领涨行业） ───
    try:
        lines.append("  ")
        lines.append("📈 领涨行业 TOP 5")
        ranking = datahub.sector_ranking("industry", 5)
        for r in ranking.get("top", [])[:5]:
            lines.append(
                f"  {r['rank']}. {r['name']}: {r['change_pct']}% "
                f"领涨:{r.get('leader','')} 涨{r.get('up_count',0)}跌{r.get('down_count',0)}"
            )
    except Exception as e:
        lines.append(f"  ⚠️ 行业数据失败: {e}")

    # ─── 维度3: 融资融券变化 ───
    try:
        lines.append("  ")
        lines.append("💰 重点个股融资趋势")
        margin_data = []
        for code in list(candidates)[:8]:
            try:
                margin = datahub.margin(code, 5)
                if margin and len(margin) >= 2:
                    latest = margin[0]['rzye']
                    prev = margin[1]['rzye']
                    change_pct = (latest / prev - 1) * 100 if prev else 0
                    if abs(change_pct) > 0.5:
                        s = "↑" if change_pct > 0 else "↓"
                        margin_data.append((code, change_pct, latest))
            except:
                pass

        margin_data.sort(key=lambda x: abs(x[1]), reverse=True)
        for code, chg, bal in margin_data[:5]:
            s = "🟢" if chg > 0 else "🔴"
            lines.append(
                f"  {code} 融资余额{bal/1e8:.1f}亿 {s}{chg:+.1f}%"
            )
        if not margin_data:
            lines.append("  无明显融资异动")
    except Exception as e:
        lines.append(f"  ⚠️ 融资数据失败: {e}")

    # ─── 维度4: 分价位段推荐 ───
    try:
        recommended = []
        for code in candidates:
            q = quotes.get(code, {})
            pe = q.get('pe_ttm', 0) or 0
            to = q.get('turnover_pct', 0) or 0
            chg = q.get('change_pct', 0) or 0
            pb = q.get('pb', 0) or 0
            price = q.get('price', 0) or 0
            # 综合打分: PE合理+流动性+涨幅
            score = 0
            if 5 <= pe <= 25:
                score += 3  # 低估值
            elif 25 < pe <= 40:
                score += 1
            if to >= 1:
                score += 2  # 高流动性
            elif to >= 0.5:
                score += 1
            if 0 < chg <= 3:
                score += 2  # 温和上涨
            elif -2 <= chg <= 0:
                score += 1  # 回调中
            if pb <= 3:
                score += 1
            recommended.append((code, score, q))

        recommended.sort(key=lambda x: x[1], reverse=True)

        # ─── 按价位段分组 ───
        tiers = {
            "💎 百元股 (100+)": [],
            "📊 中价股 (20-100)": [],
            "🔍 低价股 (0-20)": [],
        }

        for code, score, q in recommended:
            price = q.get('price', 0) or 0
            if price >= 100:
                tiers["💎 百元股 (100+)"].append((code, score, q))
            elif price >= 20:
                tiers["📊 中价股 (20-100)"].append((code, score, q))
            else:
                tiers["🔍 低价股 (0-20)"].append((code, score, q))

        total_picked = 0
        # 按用户指定分布: 低价6只、中价3只、百元2只
        allocation = [
            ("🔍 低价股 (0-20)", 6),
            ("📊 中价股 (20-100)", 3),
            ("💎 百元股 (100+)", 2),
        ]
        for tier_name, pick_n in allocation:
            tier_stocks = tiers.get(tier_name, [])
            if not tier_stocks:
                continue
            lines.append("  ")
            lines.append(f"{tier_name}")
            for i, (code, score, q) in enumerate(tier_stocks[:pick_n], 1):
                pe = q.get('pe_ttm', 0) or 0
                chg = q.get('change_pct', 0) or 0
                to = q.get('turnover_pct', 0) or 0
                pb = q.get('pb', 0) or 0
                price = q.get('price', 0) or 0
                mcap = q.get('mcap_yi', 0) or 0
                s = "⭐" if score >= 5 else ("📈" if score >= 3 else "👀")
                lines.append(
                    f"  {i}. {s} {code} {q.get('name','?'):8s} "
                    f"价{price:.1f} PE={pe:.1f} PB={pb:.1f} "
                    f"涨{chg:+.1f}% 换手{to:.1f}% 市值{mcap:.0f}亿 评分{score}"
                )
                total_picked += 1

        if total_picked == 0:
            lines.append("  无符合条件的标的")
        else:
            lines.append(f"\n  📌 共推荐 {total_picked} 只（分3档）")
    except Exception as e:
        lines.append(f"  ⚠️ 推荐计算失败: {e}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  持仓分析
# ═══════════════════════════════════════════════════════════

def _calc_stock_scores(quotes, portfolio_data):
    """综合打分 — 估值+成长+情绪+规模，截面排名

    Args:
        quotes: datahub.quotes() 结果，code→行情dict
        portfolio_data: code→{name,qty,cost,return_pct,pos_value}

    Returns:
        sorted list of dicts [{code,name,signal,emoji,total_score,rank,...}]
    """
    import numpy as np

    records = []
    codes = list(quotes.keys())
    total = len(codes)

    for code in codes:
        q = quotes.get(code, {})
        pd_info = portfolio_data.get(code, {})
        if not q or not q.get('price'):
            continue

        name = q.get('name', code)
        pe = q.get('pe_ttm') or 0
        pb = q.get('pb') or 0
        change_pct = q.get('change_pct') or 0
        turnover = q.get('turnover_pct') or 0
        vol_ratio = q.get('vol_ratio') or 1
        amplitude = q.get('amplitude_pct') or 0
        mcap = q.get('mcap_yi') or 0  # 亿元
        ret = pd_info.get('return_pct') or 0
        qty = pd_info.get('qty') or 0

        rec = {
            'code': code, 'name': name, 'qty': qty,
            'pe': pe, 'pb': pb, 'change_pct': change_pct,
            'turnover': turnover, 'vol_ratio': vol_ratio,
            'amplitude': amplitude, 'mcap': mcap,
            'holding_return': ret, 'price': q.get('price', 0),
        }

        # — 估值得分 (40%) —
        # PEG 有值时优先使用，否则用 PE fallback
        peg = None
        cagr = 0
        try:
            val = datahub.full_valuation(code)
            if val:
                peg = val.get('peg')
                cagr = val.get('cagr_pct') or 0
        except Exception:
            pass

        rec['peg'] = peg
        rec['cagr'] = cagr

        has_analyst = (peg is not None and peg > 0) or (cagr is not None and cagr != 0)
        if peg is not None and peg > 0:
            score_peg = np.clip(150 - peg * 50, 0, 100)
        elif pe > 0 and has_analyst:
            score_peg = np.clip(100 - pe * 1.5, 0, 100)
        elif pe > 0:
            score_peg = np.clip(100 - pe * 1.5, 0, 80) * 0.6  # 无分析师覆盖，估值分打折
        else:
            score_peg = 20  # 亏损且无覆盖

        score_pb = np.clip(100 - pb * 15, 0, 100) if pb > 0 else 30
        val_score = (score_peg * 0.6 + score_pb * 0.4) * 0.4

        # — 成长得分 (20%) —
        if cagr:
            score_growth = np.clip(cagr * 3, 0, 100) * 0.2
        else:
            score_growth = 0.08  # 无分析师覆盖，成长分显著降低（原0.1→0.08，使得此类股总分少2分）

        # — 情绪得分 (30%) —
        score_turn = np.clip(100 - abs(turnover - 5) * 8, 0, 100)
        if vol_ratio > 1 and change_pct > 0:
            score_vol = 80  # 放量上涨
        elif vol_ratio > 3 and change_pct < 0:
            score_vol = 20  # 放量下跌
        elif vol_ratio > 3:
            score_vol = 40  # 异常放量
        else:
            score_vol = 55
        score_amp = 80 if amplitude < 5 else (50 if amplitude < 8 else 20)
        # 区间涨跌修正（用当日涨跌幅代理，等 OHLCV 攒够后升级为20日区间收益）
        ret_adj = 0
        if change_pct > 7:
            ret_adj = -15  # 短期过热
        elif change_pct > 5:
            ret_adj = -8
        elif change_pct < -7:
            ret_adj = -10  # 恐慌破位
        elif change_pct < -5:
            ret_adj = -5
        tech_score = (score_turn * 0.3 + score_vol * 0.3 + score_amp * 0.2 + 20 * 0.2) * 0.3 + ret_adj * 0.003
        # ^ 20是基础活跃分, ret_adj 作为减分项

        # — 规模得分 (10%) —
        if mcap > 500:
            score_mcap = 80
        elif mcap > 100:
            score_mcap = 60
        else:
            score_mcap = 40
        size_score = score_mcap * 0.1

        rec['total_score'] = round(val_score + score_growth + tech_score + size_score, 1)
        rec['val_score'] = round(val_score, 1)
        rec['growth_score'] = round(score_growth, 1)
        rec['tech_score'] = round(tech_score, 1)
        rec['size_score'] = round(size_score, 1)

        records.append(rec)

    # — 截面排名 —
    records.sort(key=lambda x: x['total_score'], reverse=True)
    n = len(records)
    for i, r in enumerate(records):
        r['rank'] = i + 1
        r['rank_pct'] = round((i + 1) / n * 100)
        if i < max(5, n * 0.15):  # 前15%或至少5只
            r['signal'] = 'BUY'
            r['emoji'] = '⚡'
        elif i >= n - max(5, int(n * 0.15)):  # 后15%
            r['signal'] = 'SELL'
            r['emoji'] = '⚠️'
        else:
            r['signal'] = 'HOLD'
            r['emoji'] = '✅'

    return records


def portfolio_analysis(session_name="持仓"):
    """持仓组合分析"""
    print(f"📊 生成{ session_name }持仓分析...")
    trade_date = get_trading_date()

    lines = [
        f"## 📊 {session_name}持仓分析",
        f"📅 {trade_date}",
        "",
    ]

    # ─── 加载持仓数据（数量+成本均价+总收益率） ───
    portfolio_data = {}  # code → {name, qty, cost, return_pct, pos_value}
    try:
        from portfolio_db_pg import portfolio_db
        stocks = portfolio_db.get_all_stocks()
        for s in stocks:
            code = s.get('code', '')
            qty = int(s.get('quantity') or 0)
            cost = float(s.get('cost_price') or 0)
            portfolio_data[code] = {
                'name': s.get('name', code),
                'qty': qty,
                'cost': cost,
                'return_pct': 0,  # 拿到行情后再算
                'pos_value': cost * qty,
            }
    except Exception as e:
        print(f"⚠️ 持仓数据加载失败: {e}")

    # ─── 行情快照 ───
    try:
        portfolio_codes = get_portfolio_codes()
        quotes = datahub.quotes(portfolio_codes)
        if quotes:
            # ── 综合打分（估值+成长+情绪+规模） ──
            scores = _calc_stock_scores(quotes, portfolio_data)
            score_map = {r['code']: r for r in scores}

            # 排名摘要
            buy_list = [r for r in scores if r['signal'] == 'BUY']
            sell_list = [r for r in scores if r['signal'] == 'SELL']
            lines.append("  ")
            lines.append("📊 综合打分排名（截面排序）")
            lines.append(f"⚡ **买入 {len(buy_list)}只** | ✅ **持有 {len(scores)-len(buy_list)-len(sell_list)}只** | ⚠️ **卖出 {len(sell_list)}只**")

            # —— 买入/卖出专区 ——
            if buy_list:
                lines.append("\n### ⚡ 买入建议\n")
                lines.append("| # | 股票 | 现价 | 今日 | 持仓 | 收益 | 评分 |")
                lines.append("|---|------|------|------|------|------|------|")
                for r in buy_list:
                    pd = portfolio_data.get(r['code'], {})
                    qty = pd.get('qty', 0)
                    cost = pd.get('cost', 0)
                    price = r.get('price', 0) or quotes.get(r['code'], {}).get('price', 0) or 0
                    ret = (price - cost) / cost * 100 if cost > 0 else None
                    ret_s = f"{ret:+.1f}%" if ret is not None else "-"
                    change_s = f"{r['change_pct']:+.1f}%" if r.get('change_pct') is not None else "-"
                    qty_s = f"{qty}股" if qty > 0 else "-"
                    lines.append(f"| {r['rank']} | **{r['name']}** | {price:.2f} | {change_s} | {qty_s} | {ret_s} | {r['total_score']:.0f}{r['emoji']} |")
            if sell_list:
                lines.append("\n### ⚠️ 卖出建议\n")
                lines.append("| # | 股票 | 现价 | 今日 | 持仓 | 收益 | 评分 |")
                lines.append("|---|------|------|------|------|------|------|")
                for r in sell_list:
                    pd = portfolio_data.get(r['code'], {})
                    qty = pd.get('qty', 0)
                    cost = pd.get('cost', 0)
                    price = r.get('price', 0) or quotes.get(r['code'], {}).get('price', 0) or 0
                    ret = (price - cost) / cost * 100 if cost > 0 else None
                    ret_s = f"{ret:+.1f}%" if ret is not None else "-"
                    change_s = f"{r['change_pct']:+.1f}%" if r.get('change_pct') is not None else "-"
                    qty_s = f"{qty}股" if qty > 0 else "-"
                    lines.append(f"| {r['rank']} | **{r['name']}** | {price:.2f} | {change_s} | {qty_s} | {ret_s} | {r['total_score']:.0f}{r['emoji']} |")

            # 按涨幅分组显示
            up_stocks = []
            down_stocks = []
            for code, q in quotes.items():
                chg = q.get('change_pct', 0) or 0
                name = q.get('name', code)
                price = q.get('price', 0) or 0
                sm = score_map.get(code, {})
                # 更新持仓收益率 + 市值
                if code in portfolio_data:
                    cost = portfolio_data[code]['cost']
                    qty = portfolio_data[code]['qty']
                    if cost > 0 and qty > 0:
                        portfolio_data[code]['return_pct'] = (price - cost) / cost * 100
                        portfolio_data[code]['pos_value'] = price * qty
                    else:
                        portfolio_data[code]['pos_value'] = 0
                if chg > 0:
                    up_stocks.append((code, name, price, chg, sm))
                else:
                    down_stocks.append((code, name, price, chg, sm))

            # 涨幅榜简要
            if up_stocks:
                up_stocks.sort(key=lambda x: x[3], reverse=True)
                lines.append("\n🟢 **涨幅TOP5**")
                for code, name, price, chg, sm in up_stocks[:5]:
                    pd = portfolio_data.get(code, {})
                    qty = pd.get('qty', 0)
                    ret = pd.get('return_pct')
                    rank = sm.get('rank', '?')
                    emoji = sm.get('emoji', '? ')
                    score = sm.get('total_score', 0)
                    suffix = f" {qty}股" if qty > 0 else ""
                    suffix += f" 收{ret:+.1f}%" if ret is not None else ""
                    lines.append(
                        f"- **{name}** {price:.2f} 涨{chg:+.1f}%{suffix}"
                    )

            # 跌幅榜简要
            if down_stocks:
                down_stocks.sort(key=lambda x: x[3])
                lines.append("\n🔴 **跌幅TOP5**")
                for code, name, price, chg, sm in down_stocks[:5]:
                    pd = portfolio_data.get(code, {})
                    qty = pd.get('qty', 0)
                    ret = pd.get('return_pct')
                    rank = sm.get('rank', '?')
                    emoji = sm.get('emoji', '? ')
                    score = sm.get('total_score', 0)
                    suffix = f" {qty}股" if qty > 0 else ""
                    suffix += f" 收{ret:+.1f}%" if ret is not None else ""
                    lines.append(
                        f"- **{name}** {price:.2f} 跌{chg:+.1f}%{suffix}"
                    )

            # ─── 汇总统计 ───
            all_chg = [q.get('change_pct', 0) or 0 for q in quotes.values()]
            avg_chg = sum(all_chg) / len(all_chg) if all_chg else 0
            up_count = len([c for c in all_chg if c > 0])
            down_count = len([c for c in all_chg if c < 0])

            # 总收益统计（从持仓数据算）
            total_cost = sum(pd.get('cost', 0) * pd.get('qty', 0) for pd in portfolio_data.values())
            total_value = sum(pd.get('pos_value', 0) for pd in portfolio_data.values())
            total_return = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0

            lines.append("  ")
            lines.append("\n### 📊 统计")
            lines.append(f"观察股票数 {len(portfolio_codes)}")
            lines.append(f"持仓成本 ¥{total_cost:,.0f} | 最新市值 ¥{total_value:,.0f}")
            lines.append(f"总收益率 **{total_return:+.2f}%**")
            lines.append(f"上涨 {up_count} | 下跌 {down_count}")
            lines.append(f"平均涨跌 {avg_chg:+.2f}%")

            # 涨跌比信号
            ratio = up_count / max(down_count, 1)
            if ratio > 2:
                lines.append(f"🟢 涨跌比 {ratio:.1f}:1 — 市场强势")
            elif ratio > 1:
                lines.append(f"🟡 涨跌比 {ratio:.1f}:1 — 中性偏强")
            else:
                lines.append(f"🔴 涨跌比 1:{1/ratio:.1f} — 弱势")

    except Exception as e:
        lines.append(f"⚠️ 行情获取失败: {e}")

    # ─── 资金流向 ───
    try:
        lines.append("  ")
        lines.append("💰 资金流异动（主力净流入变化）")
        flow_anomalies = []
        for code in portfolio_codes[:10]:
            try:
                flow = datahub.capital_flow(code)
                if flow and len(flow) >= 5:
                    recent5 = sum(d['main_net'] for d in flow[-5:])
                    if abs(recent5) > 50000000:  # 5000万
                        flow_anomalies.append((code, recent5))
            except:
                pass

        flow_anomalies.sort(key=lambda x: abs(x[1]), reverse=True)
        for code, net in flow_anomalies[:8]:
            s = "🟢" if net > 0 else "🔴"
            name = quotes.get(code, {}).get('name', code)
            lines.append(
                f"  {code} {name} 近5日主力{s}{net/1e4:.0f}万"
            )
        if not flow_anomalies:
            lines.append("  无明显主力资金异动")
    except Exception as e:
        lines.append(f"  ⚠️ 资金流数据失败: {e}")

    # ─── PE/PB 估值分布 ───
    try:
        pe_vals = [q.get('pe_ttm', 0) or 0 for q in quotes.values()
                   if (q.get('pe_ttm', 0) or 0) > 0]
        pb_vals = [q.get('pb', 0) or 0 for q in quotes.values()
                   if (q.get('pb', 0) or 0) > 0]

        if pe_vals:
            lines.append("  ")
            lines.append("📈 估值分布")
            lines.append(f"  PE区间: {min(pe_vals):.1f} ~ {max(pe_vals):.1f} 中位数{np.median(pe_vals):.1f}")
            lines.append(f"  PB区间: {min(pb_vals):.2f} ~ {max(pb_vals):.2f}")
            lines.append(f"  PE<10股票: {len([p for p in pe_vals if p<10])}只")
            lines.append(f"  PE 10-25股票: {len([p for p in pe_vals if 10<=p<25])}只")
            lines.append(f"  PE 25-50股票: {len([p for p in pe_vals if 25<=p<50])}只")
    except:
        pass

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  午盘简报
# ═══════════════════════════════════════════════════════════

def noon_report():
    """午盘简报: 大盘/板块/热门股/涨跌统计"""
    lines = []
    lines.append("📊 午盘市场简报")
    lines.append(f"⏰ {datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')} CST")
    lines.append(f"{'─'*40}")

    try:
        # ─── 大盘指数 ───
        indices = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000688": "科创50",
            "sh000300": "沪深300",
        }
        idx_data = datahub.quotes(list(indices.keys()))
        lines.append("🏛️ 大盘指数")
        for code, name in indices.items():
            q = idx_data.get(code, {})
            if q and q.get("change_pct") is not None and q.get("change_pct", 0) != 0:
                price = q.get("price", "-")
                zf = q.get("change_pct", 0)
                arrow = "🔴" if float(zf) >= 0 else "🟢"
                lines.append(f"  {name} {price}  {arrow} {float(zf):+.2f}%")
            elif q:
                price = q.get("price", "-")
                lines.append(f"  {name} {price}  ⏸️")
    except Exception as e:
        lines.append(f"  ⚠️ 指数获取失败: {e}")

    try:
        # ─── 板块排名 ───
        ranking = datahub.sector_ranking("industry", 10)
        if ranking and isinstance(ranking, dict):
            lines.append("  ")
            lines.append("📂 上午板块排名")
            top_list = ranking.get("top", [])
            bottom_list = ranking.get("bottom", [])
            if top_list:
                lines.append("  🔺 涨幅前5")
                for item in top_list[:5]:
                    name = item.get("name", "")[:6]
                    pct = item.get("change_pct", 0)
                    lines.append(f"    {name} {pct:+.2f}%")
            if bottom_list:
                lines.append("  🔻 跌幅前5")
                for item in bottom_list[:5]:
                    name = item.get("name", "")[:6]
                    pct = item.get("change_pct", 0)
                    lines.append(f"    {name} {pct:+.2f}%")
    except Exception as e:
        lines.append(f"  ⚠️ 板块数据失败: {e}")

    try:
        # ─── 热门股 ───
        hot = datahub.hot_stocks()
        if hot is not None and len(hot) > 0:
            lines.append("  ")
            lines.append("🔥 热门强势股TOP10")
            top_hot = hot.head(10)
            # get_hot_stocks() 只返回 代码/名称/题材，需要单独拉实时行情补涨幅和成交额
            hot_codes = [row.get("代码", row.get("code", "")) for _, row in top_hot.iterrows()]
            hot_quotes = datahub.quotes(hot_codes) if hot_codes else {}
            for _, row in top_hot.iterrows():
                symbol = row.get("代码", row.get("code", ""))
                name = row.get("名称", row.get("name", ""))
                reason = row.get("题材归因", row.get("reason", "")) or ""
                q = hot_quotes.get(symbol, {})
                if q:
                    zf_val = q.get("change_pct", 0)
                    amount = q.get("amount_wan", 0) * 10000  # 腾讯返回万元
                else:
                    zf_val = amount = 0
                amount_str = f"{float(amount)/1e8:.1f}亿" if amount else ""
                arrow = "🔴" if zf_val >= 0 else "🟢"
                reason_short = (reason[:16] + "..") if len(reason) > 16 else reason
                lines.append(f"  {name}({symbol}) {arrow} {zf_val:+.2f}%  {amount_str}  {reason_short}")
    except Exception as e:
        lines.append(f"  ⚠️ 热门股数据失败: {e}")

    try:
        # ─── 持仓盘中概况(2026-06-12 新增:午盘一眼看持仓红绿) ───
        codes = get_portfolio_codes()
        if codes:
            pq = {}
            for i in range(0, len(codes), 20):
                try:
                    pq.update(datahub.quotes(codes[i:i + 20]) or {})
                except Exception:
                    continue
            chgs = []
            for c in codes:
                q = pq.get(c) or {}
                try:
                    chgs.append((float(q.get('change_pct') or 0), q.get('name') or c, c))
                except (TypeError, ValueError):
                    continue
            if chgs:
                up = sum(1 for ch, _, _ in chgs if ch > 0)
                down = sum(1 for ch, _, _ in chgs if ch < 0)
                avg = sum(ch for ch, _, _ in chgs) / len(chgs)
                chgs.sort(key=lambda x: x[0])
                lines.append("  ")
                lines.append("💼 持仓盘中概况")
                lines.append(f"  共{len(chgs)}只  🔴涨{up} 🟢跌{down}  平均{avg:+.2f}%")
                worst = [f"{n}{ch:+.1f}%" for ch, n, _ in chgs[:3] if ch < -2]
                best = [f"{n}{ch:+.1f}%" for ch, n, _ in chgs[-3:][::-1] if ch > 2]
                if worst:
                    lines.append(f"  ⚠️ 领跌: {'、'.join(worst)}")
                if best:
                    lines.append(f"  💪 领涨: {'、'.join(best)}")
    except Exception:
        pass

    lines.append("  ")
    lines.append(f"{'─'*40}")
    lines.append(f"⚡ Generated by 小鸡")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════

def main():
    now = datetime.now()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')} UTC] 定时分析启动")

    if not is_trading_day():
        print("🛑 非交易日，跳过分析")
        sys.exit(0)

    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    reports = []

    if mode in ("dragon_tiger", "all"):
        report = dragon_tiger_report()
        reports.append(report)

    if mode in ("morning_picks", "all"):
        report = smart_stock_picks("早盘")
        reports.append(report)
        pf = portfolio_analysis("早盘持仓")
        reports.append(pf)

    if mode in ("afternoon_picks", "all"):
        # 合并为一条综合报告（减少 14:30 消息数）
        stock_picks = smart_stock_picks("尾盘")
        pf = portfolio_analysis("尾盘持仓")
        # 去掉 pf 标题行，保留日期行后拼接
        pf_lines = pf.split("\n", 3)
        date_line = pf_lines[1] if len(pf_lines) > 1 else ""
        rest = pf_lines[3] if len(pf_lines) > 3 else ""
        merged = stock_picks + "\n\n" + date_line + "\n" + rest
        reports.append(merged)

    if mode in ("noon_report", "all"):
        report = noon_report()
        reports.append(report)

    for i, report in enumerate(reports):
        send_report(report, f"股票分析 #{i+1}")
        time.sleep(0.5)  # 避免消息过快

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 分析完成，共发送 {len(reports)} 条报告")


if __name__ == "__main__":
    main()
