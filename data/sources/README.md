# data/sources —— 原子数据源包(模块契约)

> 配套设计:[docs/数据源原子化重构计划.md](../../docs/数据源原子化重构计划.md)。
> 本包是「一家真 provider 一个模块」的落地处。门面/路由/缓存仍在 `data/datahub.py`,**本包只放直连源**。

## 铁律(每个 `sources/*.py` 必须遵守)

1. **只直连本家 provider**(HTTP/协议/官方库),不碰别家。
2. **禁止** `import akshare / adata / tushare` —— 例外仅 `sources/akshare.py`(末位兜底层)、`sources/tushare.py`(可选源)。
3. **不做跨源降级、不读缓存、不回调 datahub** —— 那是门面 `_route` + 三级缓存的职责(避免循环依赖)。
4. **任何异常吞掉返空**(空 `DataFrame` / `{}` / `[]`),**绝不抛** → 让 `datahub._route` 切下一个源。
5. **逐字段对齐契约**(见下),归一工具一律从 `_common.py` 取,不要各写一套口径。
6. 文件末尾 `if __name__ == '__main__':` 自测一两个样例(直连可达性自检)。

## 归一契约(模块按 provider **真能提供**的能力暴露,返这些格式)

- `quotes(codes) -> {code6: {price, change_pct, pe_ttm, pb, mktcap, name, ...}}`
- `kline(code, period, interval, adjust) -> DataFrame`(`DatetimeIndex` name=`'Date'` + 大写 `Open/High/Low/Close/Volume`,**volume 单位「股」**)
- `indices() -> [{name, code, price, change_pct}]`
- `financials(code, report_type) -> [dict]` · `valuation(code) -> dict` · `full_valuation(code) -> dict`
- `north_flow(days) -> [dict]` · `capital_flow(code, days) -> [dict]` · `dragon_tiger(date) -> [dict]` · `margin(code) -> [dict]`
- `sector_ranking / sector_fund_flow / sector_spot` · `news / announcements -> [dict]` · `convertible_bonds() -> [dict]` · `fund_nav(code) -> DataFrame`

> 东财能力最多;腾讯只 `quotes/indices`。模块**只实现自己真能提供的能力**。

## `_common.py` 归一工具(已就绪,直接复用)

| 工具 | 用途 |
|---|---|
| `norm_code(code)` | 剥前缀/补零 → 6 位 |
| `em_secid(code)` | 东财 secid `1./0.`(处理 900 沪B / 920 北交 / 688 科创) |
| `sina_code / tencent_code / bs_code` | 新浪 / 腾讯 / baostock 各家前缀 |
| `EM_INDEX_CODES` | 与个股重码的指数代码集(东财直连放弃) |
| `to_ohlcv(df, date_col, vol_mult)` | 列名/单位归一:大写 OCHLV + `Date` 索引 + volume→股 |
| `http_get_json / http_get_text` | 统一 UA/超时(标准库,无第三方依赖) |
| `throttle(source)` | 按源限流(复用 `rate_limiter`) |
| `ak_safe(fn, ...)` | akshare 超时/异常封装 —— **仅 `sources/akshare.py` 用** |

## 现状(迁移进度)

- **阶段 0(脚手架)✅**:本包 + `_common.py` 已建;归一工具收口完成,新写直连源即刻可用。
- **阶段 1(快赢)✅**:删 `adata`(二道贩子);K线 qfq 去 `akshare_qfq` 东财二道冗余;`dragon_tiger` 换东财数据中心直连;`capital_flow_adata` 退化为东财 canonical。
- **阶段 2-3(待续)**:把现有散在 `datahub.py` / `a_stock_data_adapter.py` 的东财/新浪/腾讯/同花顺/百度/财联社/巨潮直连**逐 provider 搬进 `sources/*.py`**,门面只留 `_route` 声明;摊平 `StockDataFetcher` 内层嵌套降级。详见重构计划 §7 阶段 2-3。

> ⚠️ 搬迁原则:**一阶段一域一验证、datahub 域函数签名/返回格式全程不变**。每域改后用
> `scripts/smoke_test_datahub_sources.py` 与改前输出逐字段对照。
</content>
</invoke>
