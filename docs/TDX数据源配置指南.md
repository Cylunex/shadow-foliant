# A 股分钟行情与 TDX 配置

Foliant 的分钟 K 线统一从 `datahub.kline()` 获取。默认路由是：

1. 配置了 Token 的 `zzshare` 正式 API；
2. 已实测返回日线、分钟线和快照，且兼容 Python 3.10+ 的 `eltdx`；
3. Python 3.12+ 环境中的 `tdx-python` 增强兜底；
4. 显式启用并另行安装后的 `easy-tdx` 与旧 `mootdx` 兼容层。

日线保持原多源行为，并把 zzshare、eltdx、tdx-python 加入独立兜底。上层不得直接调用任一 SDK。

## 配置

真实 Token、节点和网络信息只写仓库外 `.env` 或受限生产配置：

```dotenv
ZZSHARE_TOKEN=
ZZSHARE_ENABLED=true
TDX_USE_ELTDX=true
ELTDX_HOSTS=
ELTDX_TIMEOUT=8
ELTDX_POOL_SIZE=2
ELTDX_BATCH_SIZE=80
TDX_USE_TDX_PYTHON=true
TDX_PYTHON_ADDRESS=
TDX_PYTHON_TIMEOUT_MS=5000
TDX_PYTHON_START_TIMEOUT=8
TDX_PYTHON_REQUEST_TIMEOUT=10
TDX_USE_EASY_TDX=false
MARKET_DATA_MAX_BARS=3200
DATAHUB_INTRADAY_KLINE_TTL_SEC=60
```

- `ELTDX_HOSTS` 和 `TDX_PYTHON_ADDRESS` 留空时由 SDK 自动选择；固定节点只写受限生产配置。
- 设置固定节点时只在服务器受限配置中填写，不提交到仓库。
- `MARKET_DATA_MAX_BARS` 是一次逻辑请求上限；TDX 每页最多 800 条，适配层自动分页。
- zzshare 单次分钟请求受其服务配额限制，当前适配层最多请求 1000 条。
- `eltdx 0.5.1` 支持 Python 3.10+，是 NAS Python 3.11 的 TDX 主兜底。
- `tdx-python` 需要 CPython 3.12+，条件依赖会在 3.11 自动跳过；其原生 SDK 在隔离子进程运行。

## 使用

```python
from data import datahub

bars_1m = datahub.kline("600000", period="1mo", interval="1m")
bars_5m = datahub.kline("000001", period="3mo", interval="5m")
quality = datahub.kline_quality(bars_1m)
```

返回格式保持项目既有契约：`DatetimeIndex(name="Date")`，列为
`Open/Close/High/Low/Volume`，成交量统一为“股”。分钟时间采用 bar 右端点语义，例如
五分钟上午最后一根标为 11:30。

`DataFrame.attrs["datahub_source"]` 标明实际来源。分钟缓存默认 60 秒；外部源全部失败时可返回
历史缓存，但超过 10 分钟会被 `kline_quality()` 标记为不可用于实时决策。

## 边界

- TDX 是轮询行情，不是交易所推送，不用于高频交易或下单时钟。
- TCP 可连接不等于行情可用；运行健康以返回有效探针 bar 为准。easy-tdx/pytdx/xmtdx 在当前网络实测
  节点可连接但返回空，因此只保留兼容级别，不承担可用性承诺。
- 浏览器不持有 zzshare Token；Token 不进入 URL、日志、业务数据库或前端响应。
- 分钟线不在数据源层静默前复权。需要跨除权日研究时，由研究层显式合并公司行动因子。
- 公共健康检查不探测外部行情；数据源状态从 DataHub 的运行统计观察。

## 验证

```bash
python -m pytest tests/test_a_share_market_sources.py
python scripts/smoke_test_a_share_sources.py
```

冒烟脚本只输出可用性、行数、时间范围与字段，不输出 Token、节点、价格或成交数据。
