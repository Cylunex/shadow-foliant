"""向量检索 / RAG 子系统(自包含、可选、全程优雅降级)。

设计铁律:**挂了也不影响主功能**。
  - 嵌入(BGE-M3@Ollama)/ 精排(TEI rerank)/ 向量库(pgvector)任一不可用,
    本子系统的函数返回 None / [] / False,绝不把异常抛进调用方主流程。
  - 调用方(AI 分析 RAG、语义搜索页、MCP)拿到空结果就当"没有检索增强",照常工作。
  - 不被任何核心模块在 import 期强依赖;按需 `from rag import ...`。

模块:
  embed_client  嵌入 + rerank HTTP 客户端(短超时 + 失败冷却)
  store         pgvector 表 + upsert + 余弦检索
  service       semantic_search(embed→检索→rerank)+ 摄取 ingest_*
"""
