"""LLM Provider 抽象基类。所有适配器必须继承此类。"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from src.models.schemas import LLMMessage, LLMResponse


class BaseLLMProvider(ABC):
    """LLM 供应商适配器基类（必须实现）。"""

    @abstractmethod
    async def chat_completion(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> LLMResponse:
        """
        统一聊天补全接口。

        参数：
            messages: 对话消息列表
            tools: Function Calling 工具列表
            tool_choice: 工具选择策略
            temperature: 采样温度
            max_tokens: 最大 token 数
            model: 模型名（可覆盖默认）

        返回：
            LLMResponse：统一响应格式
        """
        ...

    @abstractmethod
    def get_token_count(self, text: str) -> int:
        """估算 Token 数量（用于成本计算）。"""
        ...

    @property
    @abstractmethod
    def supports_function_calling(self) -> bool:
        """该模型是否支持 Function Calling。"""
        ...

    async def chat_completion_stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> AsyncGenerator[LLMResponse, None]:
        """
        流式聊天补全（可选实现）。

        默认实现：调用非流式版本并 yield 一次。
        """
        result = await self.chat_completion(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )
        yield result
