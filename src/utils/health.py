"""启动时服务健康检查：数据库、Redis、LLM 凭证校验。"""

from __future__ import annotations

import httpx
import structlog

from src.utils.config import get_config

logger = structlog.get_logger(__name__)


async def validate_llm_credentials() -> dict[str, bool]:
    """
    启动时校验所有配置好的 LLM Provider 凭证。

    使用轻量级请求（max_tokens=1）验证 api_key 有效性。

    返回: {provider_name: is_valid}
    """
    config = get_config()
    providers_config = config.get("llm", {}).get("providers", {})
    results: dict[str, bool] = {}

    for name, provider_cfg in providers_config.items():
        api_key = provider_cfg.get("api_key", "")
        base_url = provider_cfg.get("base_url", "")

        # 跳过无 api_key 的 provider（如 ollama 本地）
        if not api_key or api_key.startswith("${"):
            logger.debug("health_skip_provider_no_key", provider=name)
            results[name] = True
            continue

        try:
            if name == "anthropic":
                valid = await _validate_anthropic(api_key)
            else:
                valid = await _validate_openai_compatible(api_key, base_url, name)
            results[name] = valid
            status = "valid" if valid else "invalid"
            logger.info("health_llm_provider_check", provider=name, status=status)
        except Exception as exc:
            results[name] = False
            logger.warning("health_llm_provider_check_failed", provider=name, error=str(exc))

    return results


async def _validate_openai_compatible(
    api_key: str,
    base_url: str,
    provider_name: str,
) -> bool:
    """校验 OpenAI 兼容格式 Provider 的凭证。"""
    if not base_url:
        base_url = "https://api.openai.com/v1"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 使用各 provider 对应的默认模型
    model_map = {
        "minimax": "MiniMax-M2.7",
        "deepseek": "deepseek-chat",
        "groq": "llama-3.1-8b-instant",
    }
    model = model_map.get(provider_name, "gpt-4o-mini")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "temperature": 0,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
        )
        # 401 = auth failed, 200 = OK
        return response.status_code == 200


async def _validate_anthropic(api_key: str) -> bool:
    """校验 Anthropic Provider 的凭证。"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-3-5-haiku-20241022",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        return response.status_code == 200
