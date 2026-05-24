"""
记忆检索与 ETL 流水线。

所有记忆读写必须通过 MemoryService，严禁直接写 SQL。
ETL 阶段：Normalize → Filter → Dedupe → Embed → Save
"""

import hashlib
import hmac
import json
import re
import uuid
from enum import Enum
from typing import Any, TypedDict

import structlog
from sqlalchemy import text

from src.core.llm.embeddings import EmbeddingFactory
from src.db import get_session
from src.utils.config import get_config

logger = structlog.get_logger(__name__)


class MemoryType(str, Enum):  # noqa: UP042
    """记忆类型枚举。"""

    EPISODIC = "episodic"    # 事件记忆：Agent 执行了什么
    PROCEDURAL = "procedural"  # 程序记忆：工具如何使用
    SEMANTIC = "semantic"    # 语义记忆：沉淀的知识
    REFLECTIVE = "reflective"  # 反思记忆：错误与纠正


# ── ETL 阶段实现 ────────────────────────────────────────────────

_SENTINEL = object()


class ETLResult(TypedDict):
    content: str
    is_duplicate: bool
    embedding: list[float] | None
    memory_id: str | None


def _normalize(content: str, max_chars: int = 10000) -> str:
    """阶段 1: Normalize — 文本清理与截断。"""
    content = content.strip()
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n[内容过长，已截断至前 {max_chars} 字符]"
    return content


# 敏感信息脱敏正则（编译后缓存）
_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*[\"']?([^\"'\\s]{1,50})[\"']?"),
        "***",
    ),
    (
        re.compile(r"(?i)(api[_-]?key|apikey)\s*[=:]\s*[\"']?([^\"'\\s]{1,50})[\"']?"),
        "***",
    ),
    (
        re.compile(r"(?i)(token|auth[_-]?token|access[_-]?token)\s*[=:]\s*[\"']?([^\"'\\s]{1,50})[\"']?"),
        "***",
    ),
    (
        re.compile(r"-----BEGIN [A-Z]+ PRIVATE KEY-----"),
        "[REDACTED PRIVATE KEY]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z]+ PUBLIC KEY-----"),
        "[REDACTED PUBLIC KEY]",
    ),
]


def _filter_sensitive(content: str) -> str:
    """阶段 2: Filter — 敏感信息脱敏。"""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        content = pattern.sub(replacement, content)
    return content


def _dedupe_content(content: str, dedupe_key: str) -> str:
    """阶段 3: Dedupe — HMAC(content_hash) 用于判断重复。"""
    return hmac.new(
        dedupe_key.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


# ── Embedding Provider ───────────────────────────────────────────────

# ── Embedding Service（模块化 Provider 链）──────────────────────────────


class EmbeddingService:
    """
    Embedding 服务（模块化 Provider 链）。

    配置驱动：
      embedding:
        chain: ["minimax", "siliconflow", "openai"]  # 按顺序尝试

    每个 Provider 均通过 EmbeddingFactory 动态创建，无需硬编码。
    全部 Provider 不可用时 embed_single() 返回 None（仅保留文本）。
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._providers: list[Any] = []  # BaseEmbeddingProvider
        self._init_providers()

    def _init_providers(self) -> None:
        """根据配置文件初始化 Provider 链。"""
        embed_cfg: dict[str, Any] = self._config.get("embedding", {})
        chain: list[str] = embed_cfg.get("chain", ["minimax", "siliconflow"])
        providers_cfg: dict[str, Any] = self._config.get("llm", {}).get("providers", {})

        for name in chain:
            # 先检查 provider 配置是否存在，避免传空 dict 导致初始化失败
            if name not in providers_cfg:
                logger.debug("embedding_provider_skipped_not_in_config", provider=name)
                continue

            cfg = providers_cfg.get(name, {})
            api_key = cfg.get("api_key", "") if isinstance(cfg, dict) else ""
            if not api_key or api_key.startswith("$"):
                logger.debug("embedding_provider_skipped_no_key", provider=name)
                continue

            try:
                provider = EmbeddingFactory.from_config(name, cfg)
                self._providers.append(provider)
                logger.info("embedding_provider_loaded", provider=name, dim=provider.get_dim())
            except Exception as exc:
                logger.warning("embedding_provider_init_failed", provider=name, error=str(exc))

        if not self._providers:
            logger.warning("embedding_no_providers_available")

    async def embed_single(self, text: str) -> list[float] | None:
        """
        阶段 4: Embed — 遍历 Provider 链，第一个成功即返回。

        均不可用时返回 None（跳过向量存储，仅保留文本）。
        """
        for provider in self._providers:
            try:
                result = await provider.embed_single(text)
                if result is not None:
                    logger.debug(
                        "embedding_provider_success",
                        provider=provider.provider_name,
                        dim=len(result),
                    )
                    return result
            except Exception as exc:
                logger.warning(
                    "embedding_provider_failed",
                    provider=provider.provider_name,
                    error=str(exc),
                )

        logger.warning("embedding_all_providers_failed")
        return None

    async def embed_batch(self, texts: list[str]) -> list[list[float]] | None:
        """
        批量 Embedding：使用第一个可用 Provider 批量接口。

        若 Provider 不支持批量，则逐条调用 embed_single()。
        """
        for provider in self._providers:
            try:
                result = await provider.embed(texts)
                if result is not None:
                    logger.debug(
                        "embedding_batch_provider_success",
                        provider=provider.provider_name,
                        count=len(result),
                    )
                    return result
            except Exception as exc:
                logger.warning(
                    "embedding_batch_provider_failed",
                    provider=provider.provider_name,
                    error=str(exc),
                )

        # Fallback：逐条调用
        results: list[list[float]] = []
        for text in texts:
            vec = await self.embed_single(text)
            if vec is None:
                return None
            results.append(vec)
        return results


# 全局 EmbeddingService 单例（延迟初始化）
_embedding_service: EmbeddingService | None = None


def get_embedding_service() -> EmbeddingService:
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service


# ── MemoryService ────────────────────────────────────────────────


class MemoryService:
    """
    记忆服务。

    禁止直接写 SQL。所有记忆操作必须通过此类。
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._embedding = get_embedding_service()

    # ── 公开 API ────────────────────────────────────────────────

    async def save(
        self,
        type: MemoryType,
        content: str,
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
        user_id: str | None = None,
        tool_name: str | None = None,
        success: bool | None = None,
    ) -> str:
        """
        保存记忆，触发完整 ETL Pipeline。

        返回新创建的 memory_id。
        """
        # ETL 阶段 1-2: Normalize + Filter
        normalized = _normalize(content, self._config["memory"]["normalize_max_chars"])
        filtered = _filter_sensitive(normalized)

        # 阶段 3: Dedupe — 检查是否已存在
        dedupe_key = self._config["memory"]["dedupe_key"]
        content_hash = _dedupe_content(filtered, dedupe_key)

        async with get_session() as session:
            # 检查重复：取同类最近记录，在 Python 里用 HMAC 比对
            dup_check = await session.execute(
                text("SELECT id, content FROM memories WHERE type = :t ORDER BY created_at DESC LIMIT 100"),
                {"t": type.value},
            )
            for row in dup_check.fetchall():
                if content_hash == _dedupe_content(row[1], dedupe_key):
                    logger.debug("memory_duplicate_skipped", memory_id=str(row[0]))
                    return str(row[0])

            # 阶段 4: Embed（使用 EmbeddingService）
            embedding: list[float] | None = None
            try:
                embedding = await self._embedding.embed_single(filtered)
            except Exception as exc:
                logger.warning("embedding_failed", error=str(exc))

            # 阶段 5: Save to PostgreSQL
            memory_id = uuid.uuid4()
            await session.execute(
                text(
                    """
                    INSERT INTO memories
                        (id, type, content, embedding, metadata, task_id, user_id, tool_name, success)
                    VALUES
                        (:id, :type, :content, :embedding, :metadata, :task_id, :user_id, :tool_name, :success)
                    """
                ),
                {
                    "id": memory_id,
                    "type": type.value,
                    "content": filtered,
                    "embedding": embedding,
                    "metadata": json.dumps(metadata or {}),
                    "task_id": uuid.UUID(task_id) if task_id else None,
                    "user_id": user_id,
                    "tool_name": tool_name,
                    "success": success,
                },
            )
            await session.commit()
            logger.info(
                "memory_saved",
                memory_id=str(memory_id),
                type=type.value,
                task_id=task_id,
            )
            return str(memory_id)

    async def search(
        self,
        query: str,
        memory_type: str = "all",
        task_id: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        检索记忆：Hybrid Search + RRF 融合排序。

        参数：
            query: 搜索文本
            memory_type: episodic | procedural | semantic | reflective | all
            task_id: 可选，限定任务
            top_k: 返回条数

        返回 top_k 条最相关记忆。
        """
        async with get_session() as session:
            # 生成查询向量（嵌入服务不可用时降级为纯关键词搜索）
            query_vec = await self._embedding.embed_single(query)
            if query_vec is None:
                logger.warning("search_embedding_failed_falling_back_to_keyword", query=query[:50])
                # 纯关键词搜索（降级路径）
                keyword_sql = text("""
                    SELECT
                        id, type, content, metadata, tool_name, created_at,
                        ts_rank(to_tsvector('english', content), plainto_tsquery('english', :query)) AS keyword_score,
                        NULL::float AS vector_score
                    FROM memories
                    WHERE content ILIKE '%' || :query || '%'
                    ORDER BY keyword_score DESC
                    LIMIT :top_k
                """)
                rows = await session.execute(keyword_sql, {"query": query, "top_k": top_k})
                results = []
                async for row in rows:  # type: ignore[attr-defined]
                    results.append({
                        "id": str(row["id"]),
                        "type": row["type"],
                        "content": row["content"],
                        "metadata": row["metadata"],
                        "tool_name": row["tool_name"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                        "vector_score": None,
                        "keyword_score": round(float(row["keyword_score"]), 4) if row["keyword_score"] else None,
                    })
                return results

            # pgvector 近似搜索（cosine distance）+ 关键词全文搜索
            # 使用 UNION ALL 融合两种结果，再用 RRF 排序
            vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"
            sql = f"""
                WITH vector_results AS (
                    SELECT
                        id, type, content, metadata, tool_name, created_at,
                        1 - (embedding <=> '{vec_str}'::vector) AS vector_score
                    FROM memories
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> '{vec_str}'::vector
                    LIMIT {top_k * 2}
                ),
                keyword_results AS (
                    SELECT
                        id, type, content, metadata, tool_name, created_at,
                        ts_rank(to_tsvector('english', content), plainto_tsquery('english', :query)) AS keyword_score
                    FROM memories
                    WHERE content ILIKE '%' || :query || '%'
                    LIMIT {top_k * 2}
                ),
                combined AS (
                    SELECT *, 0 AS source FROM vector_results
                    UNION ALL
                    SELECT *, 1 AS source FROM keyword_results
                ),
                ranked AS (
                    SELECT *,
                           ROW_NUMBER() OVER (PARTITION BY id ORDER BY vector_score DESC) AS vector_rank,
                           ROW_NUMBER() OVER (PARTITION BY id ORDER BY keyword_score DESC) AS keyword_rank
                    FROM combined
                )
                SELECT id, type, content, metadata, tool_name, created_at,
                       vector_score, keyword_score, source,
                       COALESCE(1.0 / (60 + (vector_rank + 1)), 0)
                       + COALESCE(1.0 / (60 + (keyword_rank + 1)), 0) AS rrf_score
                FROM ranked
                ORDER BY rrf_score DESC
                LIMIT {top_k}
            """
            rows = await session.execute(text(sql), {"query": query})
            results = []
            async for row in rows:  # type: ignore[attr-defined]
                results.append({
                    "id": str(row["id"]),
                    "type": row["type"],
                    "content": row["content"],
                    "metadata": row["metadata"],
                    "tool_name": row["tool_name"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "vector_score": round(row["vector_score"], 4) if row["vector_score"] else None,
                    "keyword_score": round(row["keyword_score"], 4) if row["keyword_score"] else None,
                })
            return results


# 全局单例
memory_service = MemoryService()
