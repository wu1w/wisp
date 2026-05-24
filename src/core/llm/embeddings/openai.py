"""OpenAI 兼容 Embedding Provider。"""

from typing import Any

import httpx

from src.core.llm.embeddings.base import BaseEmbeddingProvider


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """
    OpenAI 兼容格式的 Embedding 适配器。

    适用：OpenAI（text-embedding-3-small / text-embedding-3-large）、
          DeepSeek、硅基流动、Groq、阿里云百炼等所有 OpenAI 兼容端点。
    """

    DEFAULT_MODEL = "text-embedding-3-small"
    DIM = 1536  # text-embedding-3-small 默认维度

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_dim(self) -> int:
        return self.DIM

    def _is_configured(self) -> bool:
        return bool(self._api_key) and not self._api_key.startswith("$")

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]] | None:
        """
        调用 OpenAI 兼容端点的 /embeddings 接口。
        """
        if not self._is_configured():
            return None

        model = model or self._model

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "input": texts,  # OpenAI 支持批量
                    },
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

                embeddings: list[dict[str, Any]] | None = data.get("data")
                if not embeddings:
                    return None

                results: list[list[float]] = []
                for item in embeddings:
                    emb = item.get("embedding")
                    if not isinstance(emb, list):
                        return None
                    results.append(emb)
                return results

            except Exception:
                return None
