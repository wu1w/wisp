"""
Evolution Engine — 公开接口（Stub + Python async 包装）。

数据结构和核心分析算法已编译到：
    src/core/proprietary/evolution.cpython-*.so

本文 件保留：
1. async 方法（依赖 asyncpg，无法 Cython 编译）
2. DB 交互逻辑
3. 与其他 wisp 模块（prompt_manager）的对接

勿直接修改本文 件——所有修改需在
src/core/proprietary/evolution.pyx 中进行，然后重新编译。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

# ── 从编译的闭源模块导入核心数据结构 ──────────────────────────────

from src.core.proprietary import (
    OutcomeRecord,
    SegmentAnalysis,
    PromptChange,
    EvolutionProposal,
    analyze_by_profile,
    bump_version,
    summarize_analyses,
    generate_proposal_changes,
)

logger = structlog.get_logger(__name__)

# ── DB helpers ────────────────────────────────────────────────────

async def _acquire_conn():
    from src.db import get_pool
    pool = await get_pool()
    return pool.acquire()


async def _load_outcomes(profile: str | None = None, limit: int = 200) -> list[OutcomeRecord]:
    """从 DB 加载最近的 outcome 记录。"""
    async with await _acquire_conn() as conn:
        if profile:
            rows = await conn.fetch(
                "SELECT * FROM evolution_outcomes WHERE profile = $1 ORDER BY created_at DESC LIMIT $2",
                profile, limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM evolution_outcomes ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            OutcomeRecord(
                task_id=str(r["task_id"]),
                profile=r["profile"],
                success=r["success"],
                tool_calls=r["tool_calls"],
                error_count=r["error_count"],
                reflection_summary=r["reflection_summary"],
                latency_seconds=r["latency_seconds"],
                timestamp=r["created_at"],
            )
            for r in rows
        ]


async def _save_outcome(record: OutcomeRecord) -> None:
    """持久化单条 outcome 记录。"""
    async with await _acquire_conn() as conn:
        await conn.execute(
            """
            INSERT INTO evolution_outcomes
                (id, task_id, profile, success, tool_calls, error_count, reflection_summary, latency_seconds)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            uuid.UUID(record.task_id),
            record.profile,
            record.success,
            record.tool_calls,
            record.error_count,
            record.reflection_summary,
            record.latency_seconds,
        )


async def _load_proposals(approved: bool | None = None) -> list[EvolutionProposal]:
    """从 DB 加载 proposals，approved=None 返回所有。"""
    async with await _acquire_conn() as conn:
        if approved is None:
            rows = await conn.fetch("SELECT * FROM evolution_proposals ORDER BY created_at DESC")
        else:
            rows = await conn.fetch(
                "SELECT * FROM evolution_proposals WHERE approved = $1 ORDER BY created_at DESC",
                approved,
            )
        proposals = []
        for r in rows:
            analyses = [SegmentAnalysis(**a) for a in r["analyses_json"]]
            changes = [PromptChange(**c) for c in r["changes_json"]]
            proposals.append(EvolutionProposal(
                proposal_id=r["proposal_id"],
                created_at=r["created_at"],
                target_version=r["target_version"],
                new_version=r["new_version"],
                analyses=analyses,
                changes=changes,
                summary=r["summary"],
                approved=r["approved"],
                approved_at=r["approved_at"],
                applied_at=r["applied_at"],
            ))
        return proposals


async def _save_proposal(proposal: EvolutionProposal) -> None:
    """持久化提案到 DB。"""
    import json
    async with await _acquire_conn() as conn:
        await conn.execute(
            """
            INSERT INTO evolution_outcomes
                (id, proposal_id, target_version, new_version, analyses_json, changes_json, summary, approved)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            uuid.uuid4(),
            proposal.proposal_id,
            proposal.target_version,
            proposal.new_version,
            json.dumps([a.__dict__ for a in proposal.analyses]),
            json.dumps([c.__dict__ for c in proposal.changes]),
            proposal.summary,
            proposal.approved,
        )


async def _update_proposal_approval(proposal_id: str, approved: bool) -> None:
    """更新提案审批状态。"""
    async with await _acquire_conn() as conn:
        await conn.execute(
            """
            UPDATE evolution_proposals
            SET approved = $1, approved_at = NOW()
            WHERE proposal_id = $2
            """,
            approved,
            proposal_id,
        )


# ── Evolution Engine ─────────────────────────────────────────────

_MIN_SAMPLES = 10


class EvolutionEngine:
    """
    Prompt 进化引擎（DB 持久化版本）。

    核心分析算法来自 src.core.proprietary（编译为 .so）。
    async 方法依赖 asyncpg，保留在 Python 层。
    """

    async def record_outcome(
        self,
        task_id: str,
        profile: str,
        success: bool,
        tool_calls: int,
        error_count: int,
        reflection_summary: str | None,
        latency_seconds: float,
    ) -> None:
        """记录一次任务执行结果到 DB。"""
        record = OutcomeRecord(
            task_id=task_id,
            profile=profile,
            success=success,
            tool_calls=tool_calls,
            error_count=error_count,
            reflection_summary=reflection_summary,
            latency_seconds=latency_seconds,
        )
        await _save_outcome(record)
        logger.info(
            "evolution_outcome_recorded",
            task_id=task_id,
            profile=profile,
            success=success,
        )

    async def propose_evolution(self) -> EvolutionProposal | None:
        """
        基于 DB 中的历史数据生成 Prompt 进化提案。
        样本不足返回 None。
        """
        from src.core.prompts import prompt_manager

        outcomes = await _load_outcomes(limit=200)
        if len(outcomes) < _MIN_SAMPLES:
            logger.info("evolution_insufficient_samples", count=len(outcomes), need=_MIN_SAMPLES)
            return None

        # 使用编译的核心分析算法
        analyses = []
        for profile in ("coding", "chatting", "cheap"):
            analysis = analyze_by_profile(outcomes, profile)
            if analysis:
                analyses.append(analysis)

        if not analyses:
            return None

        # 使用编译的提案生成算法
        changes = generate_proposal_changes(analyses)

        current_version = prompt_manager._active_version
        new_version = bump_version(current_version)
        summary = summarize_analyses(analyses)

        proposal = EvolutionProposal(
            proposal_id=str(uuid.uuid4()),
            created_at=datetime.now(timezone.utc),
            target_version=current_version,
            new_version=new_version,
            analyses=analyses,
            changes=changes if changes else [],
            summary=summary,
        )

        await _save_proposal(proposal)
        logger.info(
            "evolution_proposal_created",
            proposal_id=proposal.proposal_id,
            new_version=new_version,
            changes=len(changes),
        )
        return proposal

    async def approve_proposal(self, proposal_id: str) -> bool:
        """人工审批通过提案。"""
        await _update_proposal_approval(proposal_id, True)
        logger.info("evolution_proposal_approved", proposal_id=proposal_id)
        return True

    async def reject_proposal(self, proposal_id: str) -> bool:
        """人工拒绝提案。"""
        await _update_proposal_approval(proposal_id, False)
        logger.info("evolution_proposal_rejected", proposal_id=proposal_id)
        return True

    async def apply_proposal(self, proposal_id: str) -> bool:
        """应用已审批的进化提案到 PromptManager。"""
        proposals = await _load_proposals(approved=True)
        proposal = next((p for p in proposals if p.proposal_id == proposal_id), None)
        if not proposal:
            logger.warning("evolution_proposal_not_found", proposal_id=proposal_id)
            return False

        if proposal.applied_at is not None:
            logger.info("evolution_already_applied", proposal_id=proposal_id)
            return True

        from src.core.prompts import prompt_manager

        new_prompts: dict[str, str] = {}
        for change in proposal.changes:
            segment = getattr(prompt_manager, f"_{change.segment_name}", None)
            if segment is None:
                continue
            current_text = segment() if callable(segment) else segment
            new_text = current_text.replace(change.before_excerpt, change.after_excerpt)
            if new_text != current_text:
                new_prompts[change.segment_name] = new_text

        if not new_prompts:
            logger.info("evolution_no_effective_changes", proposal_id=proposal_id)
            async with await _acquire_conn() as conn:
                await conn.execute(
                    "UPDATE evolution_proposals SET applied_at = NOW() WHERE proposal_id = $1",
                    proposal_id,
                )
            return True

        current_version_prompts = prompt_manager._versions.get(proposal.target_version, {})
        for name in ("base", "coding", "chatting", "cheap"):
            if name not in new_prompts:
                new_prompts[name] = current_version_prompts.get(name, "")

        prompt_manager.register_version(proposal.new_version, new_prompts)
        prompt_manager.set_active_version(proposal.new_version)

        async with await _acquire_conn() as conn:
            await conn.execute(
                "UPDATE evolution_proposals SET applied_at = NOW() WHERE proposal_id = $1",
                proposal_id,
            )

        logger.info(
            "evolution_applied",
            proposal_id=proposal_id,
            new_version=proposal.new_version,
            changes_applied=len(new_prompts),
        )
        return True

    async def get_pending_proposals(self) -> list[EvolutionProposal]:
        """返回待审批的提案。"""
        proposals = await _load_proposals(approved=None)
        return [p for p in proposals if p.approved is None]

    async def get_outcome_stats(self) -> dict[str, Any]:
        """返回当前统计数据（供监控使用）。"""
        async with await _acquire_conn() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM evolution_outcomes")
            if not total:
                return {"total_records": 0}

            by_profile = {}
            for profile in ("coding", "chatting", "cheap"):
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) as cnt,
                           SUM(CASE WHEN success THEN 1 ELSE 0 END)::float / COUNT(*) as success_rate,
                           AVG(tool_calls) as avg_tool_calls
                    FROM evolution_outcomes WHERE profile = $1
                    """,
                    profile,
                )
                if row and row["cnt"]:
                    by_profile[profile] = {
                        "count": row["cnt"],
                        "success_rate": float(row["success_rate"]),
                        "avg_tool_calls": float(row["avg_tool_calls"]),
                    }
            return {"total_records": total, "by_profile": by_profile}


# 全局单例
evolution_engine = EvolutionEngine()
