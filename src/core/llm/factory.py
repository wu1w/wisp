"""LLM 工厂：根据配置动态实例化 Provider。"""

from typing import Any

from src.core.llm.anthropic import AnthropicProvider
from src.core.llm.base import BaseLLMProvider
from src.core.llm.ollama import OllamaProvider
from src.core.llm.openai import OpenAIProvider


class LLMFactory:
    """Provider 工厂。根据 provider_name 返回对应实例。"""

    _MAP: dict[str, type[BaseLLMProvider]] = {
        "openai": OpenAIProvider,
        "minimax": OpenAIProvider,   # MiniMax 也是 OpenAI 兼容格式
        "anthropic": AnthropicProvider,
        "ollama": OllamaProvider,
    }

    @classmethod
    def register(cls, name: str, cls_: type[BaseLLMProvider]) -> None:
        """注册新的 Provider 类型（供插件扩展）。"""
        cls._MAP[name] = cls_

    @classmethod
    def get_provider(cls, provider_name: str, **kwargs: Any) -> BaseLLMProvider:
        """
        获取 Provider 实例。

        参数从配置文件中的 providers.{name} 节读取，通过 kwargs 传入。
        """
        if provider_name not in cls._MAP:
            supported = ", ".join(cls._MAP.keys())
            raise ValueError(
                f"Unsupported LLM provider: {provider_name!r}. "
                f"Supported: {supported}"
            )

        provider_cls = cls._MAP[provider_name]
        return provider_cls(**kwargs)


# ── Provider 配置加载辅助 ────────────────────────────────────

def build_provider_from_config(
    provider_name: str,
    config: dict[str, Any],
) -> BaseLLMProvider:
    """
    根据配置节创建 Provider 实例。

    config 格式（对应 config/default.yaml 中的 llm.providers.{name}）：
        openai:
          api_key: "${OPENAI_API_KEY}"
          base_url: "https://api.openai.com/v1"
        minimax:
          api_key: "${MINI…KEY}"
          base_url: "https://api.minimaxi.com/v1"
        ollama:
          base_url: "http://localhost:11434"
    """
    if provider_name == "openai":
        return OpenAIProvider(
            api_key=config["api_key"],
            base_url=config.get("base_url", "https://api.openai.com/v1"),
        )
    elif provider_name in ("minimax", "deepseek", "groq"):
        # 均为 OpenAI 兼容格式
        return OpenAIProvider(
            api_key=config["api_key"],
            base_url=config.get("base_url", ""),
        )
    elif provider_name == "anthropic":
        return AnthropicProvider(
            api_key=config["api_key"],
        )
    elif provider_name == "ollama":
        return OllamaProvider(
            base_url=config.get("base_url", "http://localhost:11434"),
        )
    else:
        return LLMFactory.get_provider(provider_name)
