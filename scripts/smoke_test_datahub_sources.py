import os, sys, io  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401  路径引导
"""datahub 并列多源 smoke-test —— 校验"新增第二源"与"主源"格式逐字段一致。

背景:2026-06-24 给 datahub 几个单源域补了并列第二源(quotes/capital_flow/stock_news/north_flow)。
_route 只会返回第一个成功的源,平时跑不到第二源,所以**必须单独把每个源各调一次、比对字段集**,
否则某天主源挂、备源顶上时字段对不齐会污染回测/因子(177 处调用方依赖逐字段一致)。

⚠️ 本脚本要在**能连行情、装了 akshare/pandas 的环境**(生产服务器)跑:
    python scripts/smoke_test_datahub_sources.py            # 默认 600519
    python scripts/smoke_test_datahub_sources.py 000001 600036

判读:
  ✅ 两源 keys 完全一致 = 安全,备源可放心顶上。
  ⚠️ 备源返回空 = 该源当前不可达(北向 akshare 大概率如此),无害但没起到兜底;记录待查。
  ❌ keys 不一致 = 危险!不要依赖该备源,把对应 _route 里的第二源摘掉或修映射,再重测。
"""

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import datahub  # noqa: E402
from a_stock_data_adapter import adapter  # noqa: E402
from data_source_manager import data_source_manager as dsm  # noqa: E402


def _keys_of(rows):
    """list[dict] → 首条 keys 集合;dict → 其 keys;其余 → set()。"""
    if isinstance(rows, dict):
        return set(rows.keys())
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return set(rows[0].keys())
    return set()


def _cmp(domain, main_rows, alt_rows, alt_name='akshare'):
    mk, ak_ = _keys_of(main_rows), _keys_of(alt_rows)
    n_main = len(main_rows) if hasattr(main_rows, '__len__') else '?'
    n_alt = len(alt_rows) if hasattr(alt_rows, '__len__') else '?'
    print(f'\n══ {domain} ══  主源 {n_main} 条 / {alt_name} {n_alt} 条')
    if not ak_:
        print(f'  ⚠️ {alt_name} 返回空 — 当前不可达,未起兜底(无害,记录待查)')
        if mk:
            print(f'     主源 keys: {sorted(mk)}')
        return
    if not mk:
        print(f'  ⚠️ 主源返回空 — 无法对比(主源此刻不可达?){alt_name} keys: {sorted(ak_)}')
        return
    missing, extra = mk - ak_, ak_ - mk
    if not missing and not extra:
        print(f'  ✅ 字段完全一致: {sorted(mk)}')
    else:
        print(f'  ❌ 字段不一致!备源缺: {sorted(missing)} | 备源多: {sorted(extra)}')
        print(f'     主源 keys: {sorted(mk)}')
        print(f'     {alt_name} keys: {sorted(ak_)}')
    # north_flow 额外核对:是否按日序列(日期是否唯一)
    if domain == 'north_flow' and isinstance(alt_rows, list) and len(alt_rows) > 1:
        dates = {r.get('trade_date') for r in alt_rows}
        if len(dates) <= 1:
            print('  ❌ akshare 北向不是按日序列(日期全同=当日汇总表)→ 已被防御拦截,符合预期')


def main():
    codes = sys.argv[1:] or ['600519']
    print(f'datahub 多源 smoke-test  codes={codes}')

    for code in codes:
        print(f'\n########## {code} ##########')
        # 1. quotes:腾讯主 vs 东财 ulist
        try:
            _cmp('quotes', adapter.get_quotes([code]), adapter.get_quotes_eastmoney([code]), '东财ulist')
        except Exception as e:
            print(f'  quotes 测试异常: {type(e).__name__}: {e}')
        # 2. capital_flow:东财 push2his 主 vs akshare
        try:
            _cmp('capital_flow', adapter.get_fund_flow_history(code, 120),
                 datahub._capital_flow_akshare(code, 120))
        except Exception as e:
            print(f'  capital_flow 测试异常: {type(e).__name__}: {e}')
        # 3. stock_news:东财搜索(dsm)主 vs akshare
        try:
            _cmp('stock_news', dsm.get_stock_news_a_stock(code, 20),
                 datahub._stock_news_akshare(code, 20))
        except Exception as e:
            print(f'  stock_news 测试异常: {type(e).__name__}: {e}')

    # 4. north_flow:全市场,只测一次
    print('\n########## 北向(全市场) ##########')
    try:
        _cmp('north_flow', dsm.get_north_flow_a_data(30), datahub._north_flow_akshare(30))
    except Exception as e:
        print(f'  north_flow 测试异常: {type(e).__name__}: {e}')

    print('\n判读:✅一致=安全 / ⚠️空=暂不可达无害 / ❌不一致=摘掉该备源或修映射')


if __name__ == '__main__':
    main()
