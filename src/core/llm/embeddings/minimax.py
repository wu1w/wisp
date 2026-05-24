"""MiniMax Embedding Provider。"""

from typing import Any

import httpx

from src.core.llm.embeddings.base import BaseEmbeddingProvider


class MiniMaxEmbeddingProvider(BaseEmbeddingProvider):
    """
    MiniMax Embedding 适配器。

    API 文档：https://www.minimaxi.com/document/Guides/Embedding
    默认模型：embo-01（输出 1024 维向量）
    """

    DEFAULT_MODEL = "embo-01"
    DIM = 1024

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "minimax"

    def get_dim(self) -> int:
        return self.DIM

    def _is_configured(self) -> bool:
        """检查 API Key 是否有效（未设置占位符）。"""
        return bool(self._api_key) and not self._api_key.startswith("$")

    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
        **kwargs: Any,
    ) -> list[list[float]] | None:
        """
        调用 MiniMax Embedding API。

        MiniMax API 支持批量，但此处采用简化逻辑：
        每次请求都传所有文本，保持批量效率。
        """
        if not self._is_configured():
            return None

        model = model or self._model

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "type": "text",
                        "texts": texts,
                    },
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

                vectors: list[dict[str, Any]] | None = data.get("vectors")
                if not vectors:
                    return None

                results: list[list[float]] = []
                for v in vectors:
                    emb = v.get("embedding")
                    if isinstance(emb, list):
                        results.append(emb)
                    else:
                        return None  # 格式异常

                return results

            except Exception:
                return None
