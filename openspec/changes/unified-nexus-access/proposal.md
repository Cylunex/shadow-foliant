# 统一 Platform 鉴权与 Nexus Agent 接入

状态：目标设计，尚未实现；2026-09-07。

## 动机

各领域重复 OIDC、Session、Agent registry、模型连接与工具循环，导致撤销、披露及确认语义不一致。用户要求统一这些公共机制，细化 Nexus 接入并修正冲突。

## 范围

Foliant 接入中央 Session、Access、Agent/MCP/Model Runtime；保留应用服务、确定性计算、PIT、冻结研究、研究任务、来源配额、持仓与历史成交事实。详细设计见 [领域设计](../../../docs/nexus-integration-design.md)。

## 兼容

现有全局主组合仍限定明确 owner/管理员，不因中央身份接入变成多租户。旧机器 scope/拒绝与冻结证据继续有效；逐能力切换，不放宽旧 API，不增加券商执行。历史 Run/Receipt/业务主键保留，中央授权失败不 fallback 到旧身份。
