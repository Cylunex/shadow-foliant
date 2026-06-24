import os, sys, io, time  # noqa: E401
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


GAP = 6   # 每个外部接口之间至少间隔 6s(>5s 防封,尤其东财各子域共用 IP)


def main():
    codes = sys.argv[1:] or ['600519']
    print(f'datahub 多源 smoke-test  codes={codes}  (每个接口间隔 {GAP}s)')
    _state = {'first': True}

    def fetch(fn):
        """取一个源:除第一次外,调用前先 sleep GAP 秒;异常返回 None。"""
        if not _state['first']:
            time.sleep(GAP)
        _state['first'] = False
        try:
            return fn()
        except Exception as e:
            print(f'  [取数异常] {type(e).__name__}: {str(e)[:80]}')
            return None

    for code in codes:
        print(f'\n########## {code} ##########')
        # 1. quotes:腾讯(a_stock)主 vs 东财 ulist vs 新浪(三源,逐个间隔取)
        q_main = fetch(lambda: adapter.get_quotes([code]))
        q_em = fetch(lambda: adapter.get_quotes_eastmoney([code]))
        q_sina = fetch(lambda: adapter.get_quotes_sina([code]))
        _cmp('quotes vs 东财ulist', q_main, q_em, '东财ulist')
        _cmp('quotes vs 新浪', q_main, q_sina, '新浪')
        print('    (新浪 PE/PB/市值 恒 0,属预期;只看 key 集是否一致)')
        # 2. capital_flow:东财 push2his 主 vs akshare(实测同源)
        cf_main = fetch(lambda: adapter.get_fund_flow_history(code, 120))
        cf_ak = fetch(lambda: datahub._capital_flow_akshare(code, 120))
        _cmp('capital_flow', cf_main, cf_ak)
        # 3. stock_news:东财搜索(dsm)主 vs akshare
        sn_main = fetch(lambda: dsm.get_stock_news_a_stock(code, 20))
        sn_ak = fetch(lambda: datahub._stock_news_akshare(code, 20))
        _cmp('stock_news', sn_main, sn_ak)

    # 4. north_flow:全市场,只测一次
    print('\n########## 北向(全市场) ##########')
    nf_main = fetch(lambda: dsm.get_north_flow_a_data(30))
    nf_ak = fetch(lambda: datahub._north_flow_akshare(30))
    _cmp('north_flow', nf_main, nf_ak)

    # 5. kline:新浪 fetcher(主源,最可达)vs mootdx 核对 同日 收盘/成交量单位 + 日期对齐
    #    用新浪而非东财作对照:东财 push2his 常被机房 IP 封,新浪更稳;且能直接验
    #    mootdx 日期是否归一化(带 15:00:00 会与共享缓存错位)。本机实测 2026-06-24:
    #    600519 近5日 收盘 5/5 一致、成交量比值 1.000(单位自适应 ×100 手→股判对)。
    print(f'\n########## K线源核对(新浪 fetcher vs 通达信 mootdx)code={codes[0]} ##########')
    try:
        import tdx_mootdx
        if not tdx_mootdx.available():
            print('  ⚠️ mootdx 未安装或无可连服务器 → datahub.kline 第三源占位(无害);'
                  '装了再测:pip install "mootdx>=0.11.0" && pip install -U "httpx>=0.27.1"')
        else:
            ref = fetch(lambda: datahub._fetcher().get_stock_data(codes[0], '6mo', '1d'))  # 新浪主源
            mx = fetch(lambda: datahub._kline_mootdx(codes[0], '6mo'))
            n_ref = 0 if ref is None else len(ref)
            n_mx = 0 if mx is None else len(mx)
            if not n_ref or not n_mx:
                print(f'  ⚠️ 新浪 {n_ref} 行 / mootdx {n_mx} 行,至少一源空,无法核对')
            else:
                common = ref.index.intersection(mx.index)
                if len(common) == 0:
                    print('  ❌ 两源无共同交易日 → mootdx 日期可能没归一化(带 15:00:00),'
                          '查 _kline_mootdx 的 .dt.normalize()')
                else:
                    print(f'  共同交易日 {len(common)}(对齐 OK)')
                    pe = ve = 0
                    for d in list(common)[-5:]:
                        rc, mc = float(ref.loc[d, 'Close']), float(mx.loc[d, 'Close'])
                        rv, mv = float(ref.loc[d, 'Volume']), float(mx.loc[d, 'Volume'])
                        r = mv / rv if rv else 0
                        p_ok = abs(rc - mc) / max(rc, 1e-9) < 0.01
                        v_ok = 0.8 < r < 1.25     # 单位对则量级≈新浪(已×100对齐"股")
                        pe += p_ok; ve += v_ok
                        print(f'    {d.date()}  收盘 新浪{rc}/mootdx{mc} {"✅" if p_ok else "❌价差大"}  '
                              f'量比值{r:.3f} {"✅单位对" if v_ok else "❌单位可能错(查 vol_mult)"}')
                    print(f'  小结:收盘 {pe}/5 一致、成交量单位 {ve}/5 对')
    except Exception as e:
        print(f'  kline 核对异常: {type(e).__name__}: {e}')

    print('\n判读:✅一致=安全 / ⚠️空=暂不可达无害 / ❌不一致=摘掉该备源或修映射/修单位')


if __name__ == '__main__':
    main()
