"""Embedding 工厂：根据配置动态实例化 Provider。"""

from typing import Any

from src.core.llm.embeddings.base import BaseEmbeddingProvider


class EmbeddingFactory:
    """
    Embedding Provider 工厂。

    使用示例：
        # 注册自定义 provider
        EmbeddingFactory.register("my-embedder", MyEmbeddingProvider)

        # 从配置创建
        provider = EmbeddingFactory.from_config("openai", config_dict)
    """

    _MAP: dict[str, type[BaseEmbeddingProvider]] = {}

    @classmethod
    def register(cls, name: str, cls_: type[BaseEmbeddingProvider]) -> None:
        """注册新的 Provider 类型（供插件扩展）。"""
        cls._MAP[name] = cls_

    @classmethod
    def available(cls) -> list[str]:
        """返回所有已注册的 provider 名称。"""
        return list(cls._MAP.keys())

    @classmethod
    def from_config(
        cls,
        provider_name: str,
        config: dict[str, Any],
        **kwargs: Any,
    ) -> BaseEmbeddingProvider:
        """
        根据配置节创建 Provider 实例。

        参数：
            provider_name: provider 标识名（如 openai, minimax, siliconflow）
            config: provider 配置字典（从 llm.providers.{name} 读取）
            **kwargs: 传给 provider 构造器的额外参数

        抛出：
            ValueError: 不支持的 provider
        """
        if provider_name not in cls._MAP:
            supported = ", ".join(cls._MAP.keys())
            raise ValueError(
                f"Unsupported embedding provider: {provider_name!r}. "
                f"Supported: {supported}. "
                f"Note: Register providers before calling from_config()."
            )

        provider_cls = cls._MAP[provider_name]
        return provider_cls(**config, **kwargs)

    @classmethod
    def create(
        cls,
        provider_name: str,
        **kwargs: Any,
    ) -> BaseEmbeddingProvider:
        """
        直接通过名称和关键字参数创建 Provider（无需预注册映射）。

        用于动态加载场景。
        """
        if provider_name not in cls._MAP:
            supported = ", ".join(cls._MAP.keys())
            raise ValueError(
                f"Unsupported embedding provider: {provider_name!r}. "
                f"Supported: {supported}"
            )
        return cls._MAP[provider_name](**kwargs)
