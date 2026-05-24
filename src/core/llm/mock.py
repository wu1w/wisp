"""Mock Provider：用于单元测试，不调用真实 API。"""

from typing import Any

from src.core.llm.base import BaseLLMProvider
from src.models.schemas import LLMMessage, LLMResponse


class MockProvider(BaseLLMProvider):
    """
    Mock LLM Provider。

    用途：
    - 单元测试（不消耗 API Key，不调用真实网络）
    - 本地演示
    - CI 流水线
    """

    def __init__(
        self,
        response_content: str = "This is a mock response.",
        tool_calls: list[dict[str, Any]] | None = None,
        raise_error: bool = False,
        error_message: str = "Mock error",
    ) -> None:
        self._response_content = response_content
        self._tool_calls = tool_calls
        self._raise_error = raise_error
        self._error_message = error_message
        self._call_count = 0

    @property
    def supports_function_calling(self) -> bool:
        return self._tool_calls is not None

    def get_token_count(self, text: str) -> int:
        return len(text) // 4

    async def chat_completion(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> LLMResponse:
        """返回预设的 Mock 响应。"""
        self._call_count += 1

        if self._raise_error:
            raise RuntimeError(self._error_message)

        return LLMResponse(
            content=self._response_content,
            tool_calls=self._tool_calls,
            usage={
                "prompt_tokens": 30,
                "completion_tokens": 20,
                "total_tokens": 50,
            },
            model=model or "mock-model",
            provider="mock",
        )

    @property
    def call_count(self) -> int:
        """记录调用次数（用于测试断言）。"""
        return self._call_count

    def reset(self) -> None:
        """重置调用计数。"""
        self._call_count = 0
