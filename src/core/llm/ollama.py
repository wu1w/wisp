"""Ollama 本地模型适配器。"""

from typing import Any

import httpx

from src.core.llm.base import BaseLLMProvider
from src.models.schemas import LLMMessage, LLMResponse


class OllamaProvider(BaseLLMProvider):
    """
    Ollama 本地模型适配器。

    适用模型：llama3, qwen, mistral 等本地运行的模型。
    注意：Ollama API 与 OpenAI 有差异，Function Calling 支持因模型而异。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        default_model: str = "llama3",
        timeout: float = 180.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout

    @property
    def supports_function_calling(self) -> bool:
        # Ollama 的函数调用支持不稳定，取决于模型
        return False

    def get_token_count(self, text: str) -> int:
        """Ollama 估算 token（按每 4 字符 1 token）。"""
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
        发送对话请求到 Ollama API。

        Ollama 使用 /api/chat 端点，格式与 OpenAI 类似但有差异。
        """
        model = model or self._default_model

        # LLMMessage → Ollama 格式
        ollama_messages = self._convert_messages(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        # Ollama 部分模型支持 tools（需模型本身支持）
        if tools:
            payload["tools"] = self._convert_tools(tools)

        headers = {"Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/api/chat",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        return self._convert_response(data)

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """将 LLMMessage 转换为 Ollama 格式。"""
        result = []
        for msg in messages:
            item: dict[str, Any] = {
                "role": msg.role,
                "content": msg.content or "",
            }
            if msg.tool_calls:
                item["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            result.append(item)
        return result

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将工具列表转换为 Ollama 格式（部分模型支持）。"""
        ollama_tools = []
        for tool in tools:
            func = tool.get("function")
            if func:
                ollama_tools.append({
                    "type": "function",
                    "function": {
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "parameters": func.get("parameters", {}),
                    },
                })
        return ollama_tools

    def _convert_response(self, data: dict[str, Any]) -> LLMResponse:
        """将 Ollama 响应转换为 LLMResponse。"""
        message = data.get("message", {})
        content = message.get("content", "")

        return LLMResponse(
            content=content,
            tool_calls=None,  # Ollama 流式不支持 tool_calls
            usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            model=data.get("model", self._default_model),
            provider="ollama",
        )
