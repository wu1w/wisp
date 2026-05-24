"""Anthropic / Claude 适配器。"""

from typing import Any

import httpx

from src.core.llm.base import BaseLLMProvider
from src.models.schemas import LLMMessage, LLMResponse


class AnthropicProvider(BaseLLMProvider):
    """
    Anthropic Claude 适配器。

    使用 Anthropic Messages API（2023-06-01）。
    注意：Anthropic API 格式与 OpenAI 有较大差异。
    """

    def __init__(
        self,
        api_key: str,
        default_model: str = "claude-sonnet-4-20250514",
        timeout: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._timeout = timeout

    @property
    def supports_function_calling(self) -> bool:
        # Claude 3.5+ 支持 Function Calling
        return True

    def get_token_count(self, text: str) -> int:
        """Anthropic token 估算（更精确需用 tiktoken）。"""
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
        发送对话请求到 Anthropic Messages API。
        """
        model = model or self._default_model

        # LLMMessage → Anthropic 格式
        anthropic_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = self._convert_tools(tools)

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        return self._convert_response(data)

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """将 LLMMessage 转换为 Anthropic 消息格式。"""
        result = []
        for msg in messages:
            if msg.role == "system":
                # Anthropic 使用单独的 system 消息
                continue
            item: dict[str, Any] = {
                "role": msg.role,
                "content": msg.content or "",
            }
            if msg.tool_calls:
                # Anthropic 的 tool_use 格式
                for tc in msg.tool_calls:
                    result.append({
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": msg.content or "",
                            },
                            {
                                "type": "tool_use",
                                "id": tc["id"],
                                "name": tc["function"]["name"],
                                "input": tc["function"]["arguments"],
                            },
                        ],
                    })
                continue
            if msg.tool_call_id:
                item["content"] = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content or "",
                    }
                ]
            result.append(item)
        return result

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将 Function Calling 工具列表转换为 Anthropic 格式。"""
        anthropic_tools = []
        for tool in tools:
            func = tool.get("function")
            if func:
                anthropic_tools.append({
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {}),
                })
        return anthropic_tools

    def _convert_response(self, data: dict[str, Any]) -> LLMResponse:
        """将 Anthropic 响应转换为 LLMResponse。"""
        content = data["content"][0]["text"]
        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            tool_calls=None,
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
            model=data.get("model", self._default_model),
            provider="anthropic",
        )
