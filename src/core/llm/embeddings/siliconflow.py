"""SiliconFlow Embedding Provider（免费额度）。"""

from typing import Any

import httpx

from src.core.llm.embeddings.base import BaseEmbeddingProvider


class SiliconFlowEmbeddingProvider(BaseEmbeddingProvider):
    """
    SiliconFlow Embedding 适配器。

    API 文档：https://docs.siliconflow.cn/api-reference/embeddings
    支持模型：BAAI/bge-small-zh-v1.5（384维）等。
    """

    DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
    DIM = 384  # bge-small-zh-v1.5

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = DEFAULT_MODEL,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "siliconflow"

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
        调用 SiliconFlow Embedding API。

        SiliconFlow API 不支持批量（input 为单字符串），
        因此逐条调用。
        """
        if not self._is_configured():
            return None

        model = model or self._model

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                # SiliconFlow 不支持批量，此处循环（可优化为并发）
                results: list[list[float]] = []
                for text in texts:
                    resp = await client.post(
                        f"{self._base_url}/embeddings",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model,
                            "input": text,
                        },
                    )
                    resp.raise_for_status()
                    data: dict[str, Any] = resp.json()
                    emb = data.get("data", [{}])[0].get("embedding")
                    if not isinstance(emb, list):
                        return None
                    results.append(emb)
                return results

            except Exception:
                return None
