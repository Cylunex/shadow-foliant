# 扶摇同花顺官方金融数据接入

## 能力边界

`fuyao_aicubes` 是直连 `https://fuyao.aicubes.cn` 的官方 REST 原子源，认证头为
`X-api-key`。当前接入交易日历、批量实时行情、日频历史行情、最新估值、财务三表、
财务指标、集合竞价与官方已开放的特色数据。

资金流接口在官方文档中仍标记为未对外开放。因此运行时能力矩阵会将
`fuyao_aicubes.capital_flow` 报告为 `degraded`，不会发送试探请求，也不会把别家数据
伪装成扶摇结果。

## 配置与密钥

推荐把密钥放在仓库外、权限为 `0600` 的简单键值文件中（`=` 或 `:` 均可）：

```dotenv
fuyao-aicubes=<真实密钥>
```

运行环境只配置：

```dotenv
FUYAO_AICUBES_ENABLED=true
FUYAO_AICUBES_API_KEY_FILE=/受保护目录/apikey.env
FUYAO_AICUBES_SECRET_KEY=fuyao-aicubes
FUYAO_AICUBES_PRIORITY_TIER=0
```

也支持受保护的进程环境变量 `FUYAO_AICUBES_API_KEY`。不要把真实值写入仓库、命令行、
日志、数据库、测试 fixture 或错误消息。路由优先级允许 `0..3`；同等级仍由健康度决定。

## 路由、口径与新鲜度

- `datahub.quotes` 首选扶摇批量快照，单批保守限制 100 只；部分返回会继续请求既有源补洞。
- `datahub.kline` 在日频 raw/qfq 链使用扶摇，分别映射 `none/forward`；历史接口只接受 `1d`，
  单次跨度不超过十年。分钟线不会误走该接口。
- 交易日历只覆盖官方返回的滚动一年窗口；窗口内生成开/休市完整证据，并与至少一个独立源
  共识。更老的历史区间仍由 zzshare 与 BaoStock 双源验证。
- 最新估值只在同一交易日收盘后进入正式估值合并，不允许回填历史日。
- 快照携带 `provider`、`request_id`、`source_timestamp`、`market_as_of`、`adjustment`、
  `currency`、`freshness` 与 `stale`。盘后当日收盘快照标记 `closing_current`，不会因离收盘已过
  数小时而误判陈旧。

## 稳定性与错误处理

客户端同时检查 HTTP 状态和业务 `code`。`2001/2003` 归类为认证/权限降级；
`3001/3002/3004` 为空或暂不可用；HTTP 429 与业务 `4001`、以及 `5001/5002/5003`
使用最多两次、指数退避且尊重 `Retry-After` 的重试。连接/读取超时分别有界，provider 级与
endpoint 级并发均受限，内存缓存按能力设置 TTL，连续失败由统一熔断器冷却。响应正文和
供应商 message 不会进入异常或状态输出。

## 只读 smoke

```bash
python scripts/fuyao_smoke.py --secret-file /受保护目录/apikey.env
```

脚本只读调用日历、两只代表性证券的批量报价、历史日线、估值和利润表。输出仅含状态、行数、
业务错误码与耗时，不打印密钥、请求头、行情值或响应正文。前三项为核心验收；估值/财务会按
账号权限独立报告 `ok` 或 `degraded`。

官方参考：

- <https://fuyao.aicubes.cn/docs/api-reference/overview/>
- <https://fuyao.aicubes.cn/docs/api-reference/prices/>
- <https://fuyao.aicubes.cn/docs/api-reference/calendar/>
- <https://fuyao.aicubes.cn/docs/api-reference/valuations/>
- <https://fuyao.aicubes.cn/docs/api-reference/auction/>
- <https://fuyao.aicubes.cn/docs/api-reference/capital-flow/>
