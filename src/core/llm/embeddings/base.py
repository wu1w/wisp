"""Embedding Provider 抽象基类。所有适配器必须继承此类。"""

from abc import ABC, abstractmethod
from typing import Any


class BaseEmbeddingProvider(ABC):
    """Embedding 供应商适配器基类（必须实现）。"""

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]] | None:
        """
        统一 Embedding 接口。

        参数：
            texts: 文本列表（支持批量以节省 API 调用）
            model: 模型名（可覆盖默认）

        返回：
            list[list[float]]：每个文本对应的向量列表
            None：表示该 provider 不可用（如未配置 API Key）
        """
        ...

    @abstractmethod
    def get_dim(self) -> int:
        """返回该 provider 模型的向量维度。"""
        ...

    @property
    def provider_name(self) -> str:
        """Provider 标识名（用于日志）。"""
        return self.__class__.__name__.replace("EmbeddingProvider", "").lower()

    async def embed_single(
        self,
        text: str,
        model: str | None = None,
        **kwargs: Any,
    ) -> list[float] | None:
        """
        单文本 Embedding（便捷方法）。

        默认实现：调用批量接口并取第一个结果。
        可被子类优化（如避免批量请求的单文本场景）。
        """
        results = await self.embed([text], model=model, **kwargs)
        if results is None or len(results) == 0:
            return None
        return results[0]
