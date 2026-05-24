"""
Dreaming API — 梦境规则查看与审批。

路由设计：
- GET  /v1/dreaming/rules        — 列出规则（支持 status 过滤）
- GET  /v1/dreaming/rules/:id    — 查看单条规则详情（含证据溯源）
- POST /v1/dreaming/rules/:id/approve — 审批通过
- POST /v1/dreaming/rules/:id/reject  — 审批拒绝
- POST /v1/dreaming/trigger      — 手动触发一次 Dreaming
- GET  /v1/dreaming/runs         — 查看历史运行记录
- GET  /v1/dreaming/stats        — 统计数据

安全：所有写操作（approve/reject）需要认证。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from src.db import acquire
from src.middleware.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/v1/dreaming", tags=["dreaming"])


# ── 响应模型 ────────────────────────────────────────────────────

class KnowledgeRule(BaseModel):
    """知识规则响应。"""

    id: str
    rule: str
    category: str
    evidence_ids: list[str]
    confidence: float
    status: str
    approved_by: str | None
    approved_at: str | None
    reject_reason: str | None
    human_note: str | None
    apply_count: int
    reject_count: int
    created_at: str


class RuleListResponse(BaseModel):
    """规则列表响应。"""

    total: int
    rules: list[KnowledgeRule]


class DreamRun(BaseModel):
    """梦境运行记录。"""

    id: str
    memory_count: int
    rule_count: int
    input_tokens: int
    output_tokens: int
    compression_ratio: float
    status: str
    rule_ids: list[str]
    error_message: str | None
    created_at: str


class DreamStats(BaseModel):
    """统计信息。"""

    total_rules: int
    pending_review: int
    approved: int
    rejected: int
    applied: int
    total_dream_runs: int
    avg_compression_ratio: float
    avg_rules_per_run: float


class TriggerResponse(BaseModel):
    """触发结果。"""

    triggered: bool
    rules_generated: int
    knowledge_base_ids: list[str]
    errors: list[str]
    input_token_count: int
    output_token_count: int


# ── 辅助函数 ───────────────────────────────────────────────────

def _rule_from_row(row: dict[str, Any]) -> KnowledgeRule:
    """将 DB 行转换为响应模型。"""
    evidence_ids_raw = row.get("evidence_ids") or []
    return KnowledgeRule(
        id=str(row["id"]),
        rule=row["rule"],
        category=row.get("category", "general"),
        evidence_ids=[str(e) for e in evidence_ids_raw],
        confidence=float(row.get("confidence") or 0.0),
        status=row["status"],
        approved_by=row.get("approved_by"),
        approved_at=row["approved_at"].isoformat() if row.get("approved_at") else None,
        reject_reason=row.get("reject_reason"),
        human_note=row.get("human_note"),
        apply_count=int(row.get("apply_count") or 0),
        reject_count=int(row.get("reject_count") or 0),
        created_at=row["created_at"].isoformat() if row.get("created_at") else "",
    )


# ── 路由 ───────────────────────────────────────────────────────

@router.get("/rules", response_model=RuleListResponse)
async def list_rules(
    status_filter: str | None = Query(default=None, alias="status"),
    category: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> RuleListResponse:
    """
    列出知识规则。

    支持按 status（pending_review / approved / rejected / applied）和
    category 过滤。
    """
    async with acquire() as conn:
        where_clauses = []
        params: list[Any] = []

        if status_filter:
            where_clauses.append("status = $1")
            params.append(status_filter)
        if category:
            idx = len(params) + 1
            where_clauses.append(f"category = ${idx}")
            params.append(category)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        limit_idx = len(params) + 1
        offset_idx = len(params) + 2

        rows = await conn.fetch(
            f"""
            SELECT * FROM knowledge_base
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *params,
            limit,
            offset,
        )

        total_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS total FROM knowledge_base WHERE {where_sql}",
            *params,
        )

    return RuleListResponse(
        total=total_row["total"] if total_row else 0,
        rules=[_rule_from_row(dict(r)) for r in rows],
    )


@router.get("/rules/{rule_id}", response_model=KnowledgeRule)
async def get_rule(rule_id: str) -> KnowledgeRule:
    """查看单条规则详情（包含证据溯源）。"""
    try:
        rule_uuid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid rule_id format")

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_base WHERE id = $1",
            rule_uuid,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

    return _rule_from_row(dict(row))


@router.post("/rules/{rule_id}/approve", response_model=KnowledgeRule)
async def approve_rule(
    rule_id: str,
    human_note: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> KnowledgeRule:
    """
    审批通过一条规则。

    通过后规则状态变为 approved，仍需显式 apply 才会生效。
    """
    try:
        rule_uuid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid rule_id format")

    now = datetime.now(UTC)

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_base WHERE id = $1",
            rule_uuid,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

        if row["status"] != "pending_review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Rule is already '{row['status']}', cannot approve",
            )

        await conn.execute(
            """
            UPDATE knowledge_base
            SET status = 'approved',
                approved_at = $1,
                approved_by = $2,
                human_note = $3,
                apply_count = apply_count + 1
            WHERE id = $4
            """,
            now,
            rule_uuid,  # 简化：用 rule_id 代替实际 user_id
            human_note,
            rule_uuid,
        )
        await conn.commit()

        updated = await conn.fetchrow(
            "SELECT * FROM knowledge_base WHERE id = $1",
            rule_uuid,
        )

    return _rule_from_row(dict(updated))


@router.post("/rules/{rule_id}/reject", response_model=KnowledgeRule)
async def reject_rule(
    rule_id: str,
    reason: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> KnowledgeRule:
    """
    审批拒绝一条规则。

    拒绝后可选择填写拒绝原因。
    """
    try:
        rule_uuid = uuid.UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid rule_id format")

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM knowledge_base WHERE id = $1",
            rule_uuid,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rule not found")

        if row["status"] != "pending_review":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Rule is already '{row['status']}', cannot reject",
            )

        await conn.execute(
            """
            UPDATE knowledge_base
            SET status = 'rejected',
                reject_reason = $1,
                reject_count = reject_count + 1
            WHERE id = $2
            """,
            reason,
            rule_uuid,
        )
        await conn.commit()

        updated = await conn.fetchrow(
            "SELECT * FROM knowledge_base WHERE id = $1",
            rule_uuid,
        )

    return _rule_from_row(dict(updated))


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_dreaming() -> TriggerResponse:
    """
    手动触发一次 Dreaming 运行。

    结果为 pending_review 状态的规则列表。
    注意：DREAMING.md 报告需单独生成。
    """
    from src.core.dreaming.worker import DreamWorker

    worker = DreamWorker()
    result = await worker.run()

    return TriggerResponse(
        triggered=result.triggered,
        rules_generated=result.rules_generated,
        knowledge_base_ids=result.knowledge_base_ids,
        errors=result.errors,
        input_token_count=result.input_token_count,
        output_token_count=result.output_token_count,
    )


@router.get("/runs", response_model=list[DreamRun])
async def list_dream_runs(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[DreamRun]:
    """查看历史 Dreaming 运行记录。"""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM dream_runs
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )

    return [
        DreamRun(
            id=str(r["id"]),
            memory_count=r["memory_count"],
            rule_count=r["rule_count"],
            input_tokens=r["input_tokens"],
            output_tokens=r["output_tokens"],
            compression_ratio=float(r["compression_ratio"] or 0.0),
            status=r["status"],
            rule_ids=[str(rid) for rid in (r["rule_ids"] or [])],
            error_message=r.get("error_message"),
            created_at=r["created_at"].isoformat() if r.get("created_at") else "",
        )
        for r in rows
    ]


@router.get("/stats", response_model=DreamStats)
async def get_dreaming_stats() -> DreamStats:
    """返回 Dreaming 统计信息。"""
    async with acquire() as conn:
        # 规则统计
        total_row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM knowledge_base")
        pending_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM knowledge_base WHERE status = 'pending_review'"
        )
        approved_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM knowledge_base WHERE status = 'approved'"
        )
        rejected_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM knowledge_base WHERE status = 'rejected'"
        )
        applied_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM knowledge_base WHERE status = 'applied'"
        )

        # 运行统计
        runs_row = await conn.fetchrow(
            "SELECT COUNT(*) AS cnt FROM dream_runs"
        )
        avg_row = await conn.fetchrow(
            """
            SELECT
                AVG(compression_ratio) AS avg_ratio,
                AVG(rule_count) AS avg_rules
            FROM dream_runs WHERE status = 'success'
            """
        )

    return DreamStats(
        total_rules=int(total_row["cnt"]) if total_row else 0,
        pending_review=int(pending_row["cnt"]) if pending_row else 0,
        approved=int(approved_row["cnt"]) if approved_row else 0,
        rejected=int(rejected_row["cnt"]) if rejected_row else 0,
        applied=int(applied_row["cnt"]) if applied_row else 0,
        total_dream_runs=int(runs_row["cnt"]) if runs_row else 0,
        avg_compression_ratio=float(avg_row["avg_ratio"] or 0.0) if avg_row else 0.0,
        avg_rules_per_run=float(avg_row["avg_rules"] or 0.0) if avg_row else 0.0,
    )
