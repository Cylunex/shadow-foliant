# Research valuation requirements

## 同日字段契约

估值补充 MUST 核实交易日期和字段定义，市值统一为亿元。低优先级源 MUST NOT 覆盖
更高优先级的有效值。无法确认口径的 PE MUST NOT 用作 PE-TTM。

Scenario: 主源 PE/PB 正常但缺市值，腾讯同日收盘报价补市值且不改原 PE/PB。
Scenario: 请求前日而实时源返回当日数据，不能入库为前日估值。

## 有界与恢复

BaoStock MUST 共用全主机串行锁和每日计数；超过本地安全预算前停止。
补跑 MUST 保存部分进度并只请求缺项；每个复合检查点 MUST 可通过不可变 manifest 重放。

Scenario: 首轮仅完成部分 PE/PB，第二轮只补未完成股票；首轮 manifest 重放结果不变。
Scenario: 只有 PB 的行保存为 partial，但不进入正式可用估值覆盖率。
Scenario: 当日部分数据不足，选择上一交易日整份有效快照，不混合两个日期。
Scenario: 500 日初始化只使用批量源，不展开逐股循环。
