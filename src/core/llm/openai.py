"""OpenAI 兼容适配器。

适用对象：OpenAI、DeepSeek、硅基流动、Groq、阿里云百炼、MiniMax 等
所有基于 OpenAI Chat Completions API 的服务商。
"""

from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

from src.core.llm.base import BaseLLMProvider
from src.models.schemas import LLMMessage, LLMResponse


class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI 兼容适配器。

    使用 httpx（异步）直接调用兼容端点，
    不依赖 openai SDK，避免版本冲突。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        default_model: str = "gpt-4o-mini",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout

    @property
    def supports_function_calling(self) -> bool:
        return True

    def get_token_count(self, text: str) -> int:
        """
        粗略估算 token 数（每 4 字符约等于 1 token）。

        精确计算需要 tiktoken 等库，此处用于成本估算。
        """
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
        """
        发送对话请求到 OpenAI 兼容端点。
        """
        model = model or self._default_model

        # 将 LLMMessage 转换为 OpenAI 格式
        openai_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            # 转换为 OpenAI 标准格式：{"type": "function", "function": {...}}
            openai_tools = []
            for t in tools:
                if "type" in t and t["type"] == "function":
                    openai_tools.append(t)
                elif "name" in t:
                    openai_tools.append({"type": "function", "function": t})
                else:
                    openai_tools.append(t)
            payload["tools"] = openai_tools
            payload["tool_choice"] = tool_choice or "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.error("minimax_400_detail", status_code=exc.response.status_code, body=exc.response.text[:500])
                raise
            data = response.json()

        return self._convert_response(data, provider="openai-compatible")

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """将 LLMMessage 列表转换为 OpenAI 消息格式。"""
        result = []
        for msg in messages:
            item: dict[str, Any] = {
                "role": msg.role,
            }
            if msg.content is not None:
                item["content"] = msg.content
            if msg.tool_calls:
                item["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            result.append(item)
        return result

    def _convert_response(
        self,
        data: dict[str, Any],
        provider: str,
    ) -> LLMResponse:
        """将 OpenAI 响应转换为统一的 LLMResponse。"""
        choice = data["choices"][0]
        message = choice["message"]

        content = message.get("content")
        tool_calls = message.get("tool_calls")

        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
            model=data.get("model", self._default_model),
            provider=provider,
        )
