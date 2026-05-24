"""
Embedding Provider 模块。

所有 Embedding Provider 必须：
1. 继承 BaseEmbeddingProvider
2. 在本文 件中注册到 EmbeddingFactory

使用示例：
    from src.core.llm.embeddings import EmbeddingFactory

    # 从配置创建
    provider = EmbeddingFactory.from_config("minimax", config)

    # 单文本便捷接口
    vec = await provider.embed_single("hello world")
"""

from src.core.llm.embeddings.base import BaseEmbeddingProvider
from src.core.llm.embeddings.factory import EmbeddingFactory

# ── 注册内置 Providers ──────────────────────────────────────────

from src.core.llm.embeddings.minimax import MiniMaxEmbeddingProvider
from src.core.llm.embeddings.siliconflow import SiliconFlowEmbeddingProvider
from src.core.llm.embeddings.openai import OpenAIEmbeddingProvider

EmbeddingFactory.register("minimax", MiniMaxEmbeddingProvider)
EmbeddingFactory.register("siliconflow", SiliconFlowEmbeddingProvider)
EmbeddingFactory.register("openai", OpenAIEmbeddingProvider)

__all__ = [
    "BaseEmbeddingProvider",
    "EmbeddingFactory",
    "MiniMaxEmbeddingProvider",
    "SiliconFlowEmbeddingProvider",
    "OpenAIEmbeddingProvider",
]
