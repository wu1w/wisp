"""
Dreaming Worker — 离线知识蒸馏执行器。

触发时机：
- 定时批量（由 cron 触发，或 scheduler 空闲时调用）
- 绝不实时处理，每条记忆由 ETL Pipeline 独立处理

安全约束：
- 只读 memories 表，不修改任何数据
- 使用 profiles.cheap 模型（gpt-4o-mini）
- 产出写入 knowledge_base，状态默认为 pending_review
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from src.core.dreaming.prompt import get_dream_prompt
from src.core.dreaming.validator import DreamValidator
from src.db import acquire
from src.utils.config import get_config

logger = structlog.get_logger(__name__)


class DreamRunResult:
    """单次 Dreaming 运行结果。"""

    def __init__(
        self,
        triggered: bool,
        rules_generated: int = 0,
        knowledge_base_ids: list[str] | None = None,
        errors: list[str] | None = None,
        input_token_count: int = 0,
        output_token_count: int = 0,
    ) -> None:
        self.triggered = triggered
        self.rules_generated = rules_generated
        self.knowledge_base_ids = knowledge_base_ids or []
        self.errors = errors or []
        self.input_token_count = input_token_count
        self.output_token_count = output_token_count


class DreamWorker:
    """
    梦境执行器。

    流程：
      1. 从 memories 表加载最近 N 小时的 episodic 记忆（摘要形式，省 token）
      2. 调用 LLM (cheap profile) 进行模式归纳
      3. Validator 熵减检查
      4. 通过的规则写入 knowledge_base (pending_review)
      5. 记录 dream_run 日志
    """

    # 触发阈值：至少需要这么多条记忆才运行
    MIN_MEMORY_COUNT = 10
    # 单次处理的最大记忆条数（防止 token 爆炸）
    MAX_MEMORIES_PER_RUN = 200
    # 每次做梦最多提炼的规则数
    MAX_RULES_PER_RUN = 5
    # 记忆时间窗口（小时）
    MEMORY_WINDOW_HOURS = 24

    def __init__(self) -> None:
        self._config = get_config()
        self._validator = DreamValidator()
        self._memory_cfg = self._config["memory"]

    async def run(self, memory_window_hours: int | None = None) -> DreamRunResult:
        """
        执行一次完整的 Dreaming 流程。

        参数：
            memory_window_hours: 记忆时间窗口（默认 24 小时）
        """
        window = memory_window_hours or self.MEMORY_WINDOW_HOURS
        logger.info("dream_worker_started", window_hours=window)

        errors: list[str] = []
        input_tokens = 0
        output_tokens = 0
        kb_ids: list[str] = []

        try:
            # ── 1. 加载记忆 ────────────────────────────────────
            memories = await self._load_recent_memories(hours=window)
            memory_count = len(memories)

            if memory_count < self.MIN_MEMORY_COUNT:
                logger.info(
                    "dream_worker_skipped",
                    reason=f"Only {memory_count} memories, need >= {self.MIN_MEMORY_COUNT}",
                )
                return DreamRunResult(triggered=False)

            # 截断到上限
            if memory_count > self.MAX_MEMORIES_PER_RUN:
                memories = memories[: self.MAX_MEMORIES_PER_RUN]
                logger.info("dream_worker_memory_truncated", count=len(memories))

            # ── 2. 构造 prompt ─────────────────────────────────
            memories_content = self._serialize_memories(memories)
            system_prompt, user_prompt = get_dream_prompt(
                memory_count=len(memories),
                memories_content=memories_content,
                max_rules=self.MAX_RULES_PER_RUN,
            )

            # 粗略估算 token（中文约 1.5 chars/token，英文约 4 chars/token）
            # 这里用字符数 / 4 作为估算，实际 token数以 LLM 返回的 usage 为准
            input_tokens = self._estimate_tokens(memories_content)

            # ── 3. 调用 LLM (cheap profile) ───────────────────
            llm_output, usage = await self._call_llm(system_prompt, user_prompt)
            output_tokens = usage.get("total_tokens", self._estimate_tokens(llm_output))

            # ── 4. 验证产出 ────────────────────────────────────
            validation = self._validator.validate(
                output_content=llm_output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

            if not validation.valid:
                errors.extend(validation.reasons)
                logger.warning(
                    "dream_validation_rejected",
                    reasons=validation.reasons,
                    compression_ratio=validation.compression_ratio,
                )
                return DreamRunResult(
                    triggered=True,
                    errors=errors,
                    input_token_count=input_tokens,
                    output_token_count=output_tokens,
                )

            # ── 5. 写入 knowledge_base ─────────────────────────
            evidence_ids = [m["id"] for m in memories]
            kb_ids = await self._save_knowledge_rules(
                rules=validation.rules,
                evidence_ids=evidence_ids,
            )

            # ── 6. 记录 dream_run 日志 ─────────────────────────
            await self._log_dream_run(
                memory_count=len(memories),
                rule_count=len(kb_ids),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                compression_ratio=(
                    input_tokens / output_tokens if output_tokens > 0 else 0.0
                ),
            )

            logger.info(
                "dream_worker_completed",
                rules_saved=len(kb_ids),
                compression_ratio=(
                    f"{input_tokens / output_tokens:.1f}" if output_tokens else "N/A"
                ),
            )

            return DreamRunResult(
                triggered=True,
                rules_generated=len(kb_ids),
                knowledge_base_ids=kb_ids,
                input_token_count=input_tokens,
                output_token_count=output_tokens,
            )

        except Exception as exc:
            logger.exception("dream_worker_error", error=str(exc))
            errors.append(str(exc))
            return DreamRunResult(
                triggered=True,
                errors=errors,
                input_token_count=input_tokens,
                output_token_count=output_tokens,
            )

    # ── 内部方法 ──────────────────────────────────────────────

    async def _load_recent_memories(
        self,
        hours: int,
    ) -> list[dict[str, Any]]:
        """
        加载最近的 episodic 记忆（摘要形式，省 token）。

        只取 id / content / tool_name / created_at，避免拉取 embedding 浪费。
        """
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, tool_name, metadata, created_at
                FROM memories
                WHERE type = 'episodic'
                  AND created_at >= $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                cutoff,
                self.MAX_MEMORIES_PER_RUN,
            )

        return [
            {
                "id": str(row["id"]),
                "content": row["content"],
                "tool_name": row["tool_name"],
                "metadata": row["metadata"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else "",
            }
            for row in rows
        ]

    def _serialize_memories(self, memories: list[dict[str, Any]]) -> str:
        """
        将记忆列表序列化为文本（供 LLM 分析）。

        格式：每条记忆一行 JSON，方便 LLM 解析。
        内容做预截断（每条最多 200 字符），防止 token 爆炸。
        """
        lines: list[str] = []
        for m in memories:
            # 预截断 content
            content = m["content"][:200] if len(m["content"]) > 200 else m["content"]
            tool = m.get("tool_name") or "unknown"
            entry = {
                "id": m["id"][:8],
                "tool": tool,
                "content": content,
            }
            lines.append(json.dumps(entry, ensure_ascii=False))
        return "\n".join(lines)

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> tuple[str, dict[str, int]]:
        """
        调用 LLM (cheap profile) 进行知识蒸馏。

        返回 (output_text, usage_dict)。
        """
        from src.core.llm.gateway import LLMGateway
        from src.models.schemas import LLMMessage

        gateway = LLMGateway()
        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=user_prompt),
        ]

        response = await gateway.chat(
            messages=messages,
            profile="cheap",  # 强制使用便宜模型
            tools=None,
            temperature=0.1,
            max_tokens=1024,
        )

        usage = dict(response.usage) if response.usage else {}
        return response.content or "", usage

    async def _save_knowledge_rules(
        self,
        rules: list[dict[str, Any]],
        evidence_ids: list[str],
    ) -> list[str]:
        """
        将验证通过的规则写入 knowledge_base 表。

        所有规则初始状态为 pending_review（未经人类审批不得生效）。
        """
        kb_ids: list[str] = []

        async with acquire() as conn:
            for rule_obj in rules:
                kb_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO knowledge_base
                        (id, rule, evidence_ids, confidence, status, created_at)
                    VALUES ($1, $2, $3, $4, 'pending_review', NOW())
                    """,
                    kb_id,
                    rule_obj["rule"],
                    [uuid.UUID(eid) for eid in evidence_ids],
                    rule_obj["confidence"],
                )
                kb_ids.append(str(kb_id))

        return kb_ids

    async def _log_dream_run(
        self,
        memory_count: int,
        rule_count: int,
        input_tokens: int,
        output_tokens: int,
        compression_ratio: float,
    ) -> None:
        """记录 dream_run 日志（存储在 knowledge_base.dream_runs 模拟表）。"""
        async with acquire() as conn:
            run_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO dream_runs
                    (id, memory_count, rule_count, input_tokens, output_tokens,
                     compression_ratio, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
                """,
                run_id,
                memory_count,
                rule_count,
                input_tokens,
                output_tokens,
                compression_ratio,
                "success" if rule_count > 0 else "no_rules",
            )

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """
        粗略估算 token 数。

        中文字符约 1.5 chars/token，英文约 4 chars/token。
        混合文本取折中值 3 chars/token。
        """
        return max(1, len(text) // 3)
