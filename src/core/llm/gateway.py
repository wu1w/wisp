"""LLM 网关：Agent Core 只和 Gateway 对话，Gateway 负责路由和负载均衡。"""

from typing import Any

import structlog

from src.core.llm.base import BaseLLMProvider
from src.core.llm.factory import build_provider_from_config
from src.models.schemas import LLMMessage, LLMResponse
from src.utils.tracing import traced

logger = structlog.get_logger(__name__)


class LLMGateway:
    """
    LLM 网关（Facade）。

    Agent Core 只调用此类，不直接接触任何 Provider 实现。
    职责：
    - 根据 profile 选择 Provider 和模型
    - 统一错误处理和降级（Fallback）
    - 追踪埋点（tracing）
    """

    def __init__(self) -> None:
        self._providers: dict[str, BaseLLMProvider] = {}
        self._config = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        """从配置加载 LLM profiles。"""
        try:
            from src.utils.config import get_config
            llm_cfg: dict[str, Any] = get_config().get("llm", {})
            return llm_cfg
        except Exception:
            logger.warning("llm_config_load_failed_using_defaults")
            return {}

    def _get_provider(self, provider_name: str) -> BaseLLMProvider:
        """获取或创建 Provider 实例。"""
        if provider_name not in self._providers:
            providers_cfg = self._config.get("providers", {})
            provider_cfg = providers_cfg.get(provider_name, {})
            self._providers[provider_name] = build_provider_from_config(provider_name, provider_cfg)
        return self._providers[provider_name]

    @traced("llm.gateway.chat_completion", "llm")
    async def chat(
        self,
        messages: list[LLMMessage],
        profile: str = "coding",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """
        统一聊天接口。

        参数：
            messages: 对话消息列表
            profile: 配置档位（对应 config 中的 profiles）
            tools: Function Calling 工具列表
            tool_choice: 工具选择策略
            temperature: 采样温度（None 则使用 profile 默认值）
            max_tokens: 最大 token 数（None 则使用 profile 默认值）

        返回：
            LLMResponse：统一响应格式
        """
        profiles = self._config.get("profiles", {})
        profile_cfg = profiles.get(profile, profiles.get("coding", {}))

        provider_name = profile_cfg.get("provider", "openai")
        model_name = profile_cfg.get("model", "gpt-4o-mini")
        temperature = temperature if temperature is not None else profile_cfg.get("temperature", 0.3)
        max_tokens = max_tokens or profile_cfg.get("max_tokens", 4096)

        provider = self._get_provider(provider_name)

        logger.debug(
            "llm_gateway_request",
            profile=profile,
            provider=provider_name,
            model=model_name,
            msg_count=len(messages),
        )

        try:
            response = await provider.chat_completion(
                messages=messages,
                model=model_name,
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            logger.debug(
                "llm_gateway_response",
                provider=provider_name,
                model=model_name,
                usage=response.usage,
            )
            return response

        except Exception as exc:
            logger.exception(
                "llm_gateway_error",
                provider=provider_name,
                model=model_name,
                error=str(exc),
            )

            # ── 降级策略 ──────────────────────────────────────────
            # 1. 如果是 400 错误且带了 tools，清除 tool result 消息后重试
            #    （MiniMax 对 tool result 消息格式敏感，第二轮需清理历史）
            is_400 = getattr(exc, "status_code", 0) == 400 or "400" in str(exc)
            if is_400 and tools:
                # 过滤掉 role=tool 的消息（MiniMax 会在无 tools 时拒绝这些消息）
                clean_messages = [
                    m for m in messages
                    if m.role != "tool"
                ]
                # 同时移除 assistant 消息中的 tool_calls（避免残留）
                clean_messages = [
                    LLMMessage(
                        role=m.role,
                        content=m.content,
                        tool_calls=None,  # 强制清除，防止 MiniMax 混淆
                        tool_call_id=m.tool_call_id,
                    )
                    for m in clean_messages
                ]
                logger.info(
                    "llm_retry_without_tools",
                    provider=provider_name,
                    model=model_name,
                    reason="400 on tools, retrying with clean history",
                    original_msg_count=len(messages),
                    clean_msg_count=len(clean_messages),
                )
                try:
                    response = await provider.chat_completion(
                        messages=clean_messages,
                        model=model_name,
                        tools=None,
                        tool_choice=None,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    return response
                except Exception as retry_exc:
                    logger.warning("llm_retry_without_tools_failed", error=str(retry_exc))
                    # 重试失败，继续走 fallback

            # 2. Fallback 到备选 provider
            fallback = profile_cfg.get("fallback_provider")
            if fallback and fallback != provider_name:
                logger.info("llm_gateway_fallback", from_=provider_name, to=fallback)
                fallback_provider = self._get_provider(fallback)
                return await fallback_provider.chat_completion(
                    messages=messages,
                    model=profile_cfg.get("fallback_model", model_name),
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            raise

    async def chat_stream(
        self,
        messages: list[LLMMessage],
        profile: str = "coding",
        tools: list[dict[str, Any]] | None = None,
        **kwargs,
    ):
        """
        流式聊天接口（返回 AsyncGenerator）。

        默认实现：调用非流式版本并 yield 一次。
        """
        result = await self.chat(messages=messages, profile=profile, tools=tools, **kwargs)
        yield result
