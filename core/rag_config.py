"""RAG 全局开关。

RAG 已退出默认链路；保留代码和历史向量，只有显式设置 RAG_ENABLED=true 才启用。
"""

import os


def is_rag_enabled() -> bool:
    return os.getenv('RAG_ENABLED', 'false').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )
