"""
人工审批 API 路由。

提供审批查询和操作接口。
"""

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from src.db import acquire
from src.middleware.auth import AuthenticatedUser, get_current_user

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── 响应模型 ────────────────────────────────────────────────────

class ApprovalRequest(BaseModel):
    """审批请求。"""

    id: str
    task_id: str
    action: str
    description: str | None
    payload: dict | None
    severity: str
    status: str
    approver_comment: str | None
    created_at: str
    expires_at: str


class ApprovalListResponse(BaseModel):
    """审批列表响应。"""

    total: int
    items: list[ApprovalRequest]


class ApprovalActionResponse(BaseModel):
    """审批操作响应。"""

    approval_id: str
    action: str
    new_status: str
    responded_at: str


# ── 路由 ─────────────────────────────────────────────────────────

@router.get("/pending", response_model=ApprovalListResponse)
async def list_pending_approvals(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalListResponse:
    """
    列出所有待审批请求。

    按 created_at 升序（最早的最先处理）。
    """
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, task_id, action, description, payload,
                   severity, status, approver_comment, created_at, expires_at
            FROM approval_requests
            WHERE status = 'pending'
              AND expires_at > NOW()
            ORDER BY created_at ASC
            LIMIT $1 OFFSET $2
            """,
            [limit, offset],
        )

        count_row = await conn.fetchrow(
            """
            SELECT COUNT(*) AS total
            FROM approval_requests
            WHERE status = 'pending' AND expires_at > NOW()
            """,
        )

    items = [
        ApprovalRequest(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            action=row["action"],
            description=row["description"],
            payload=row["payload"],
            severity=row["severity"],
            status=row["status"],
            approver_comment=row["approver_comment"],
            created_at=row["created_at"].isoformat(),
            expires_at=row["expires_at"].isoformat(),
        )
        for row in rows
    ]

    return ApprovalListResponse(
        total=count_row["total"],
        items=items,
    )


@router.post("/{approval_id}/approve", response_model=ApprovalActionResponse)
async def approve(
    approval_id: str,
    comment: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalActionResponse:
    """
    审批通过。

    流程：
    1. 验证 approval_request 存在且状态为 pending
    2. 更新状态为 approved + 填写审批意见
    3. 将对应 task_step 状态从 awaiting_approval 恢复为 pending
    4. 将 task status 恢复为 running
    """
    try:
        approval_uuid = uuid.UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid approval_id format")

    now = datetime.now(UTC)

    async with acquire() as conn:
        # 验证审批请求
        row = await conn.fetchrow(
            "SELECT * FROM approval_requests WHERE id = $1",
            [approval_uuid],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")

        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Approval already {row['status']}",
            )

        if row["expires_at"] < now:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Approval request has expired",
            )

        task_uuid = row["task_id"]

        # 更新审批请求状态
        await conn.execute(
            """
            UPDATE approval_requests
            SET status = 'approved',
                approver_comment = $1,
                responded_at = $2
            WHERE id = $3
            """,
            [comment, now, approval_uuid],
        )

        # 恢复对应 task_step（查找最新的 awaiting_approval 的 step）
        await conn.execute(
            """
            UPDATE task_steps
            SET state = 'pending', updated_at = $1
            WHERE task_id = $2 AND state = 'awaiting_approval'
            ORDER BY seq DESC
            LIMIT 1
            """,
            [now, task_uuid],
        )

        # 恢复任务状态
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'running', updated_at = $1
            WHERE id = $2
            """,
            [now, task_uuid],
        )

        # 记录主人介入事件
        await conn.execute(
            """
            INSERT INTO task_steer_events (id, task_id, message)
            VALUES ($1, $2, $3)
            """,
            [uuid.uuid4(), task_uuid, f"Approved ({approval_id}): {comment or 'no comment'}"],
        )

        await conn.commit()

    logger.info(
        "approval_approved_via_api",
        approval_id=approval_id,
        task_id=str(task_uuid),
        comment=comment,
    )

    return ApprovalActionResponse(
        approval_id=approval_id,
        action="approve",
        new_status="approved",
        responded_at=now.isoformat(),
    )


@router.post("/{approval_id}/reject", response_model=ApprovalActionResponse)
async def reject(
    approval_id: str,
    comment: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalActionResponse:
    """
    审批拒绝。

    流程：
    1. 验证审批请求
    2. 更新状态为 rejected
    3. 将 task status 置为 failed
    4. 所有 pending/running steps 标记为 dead
    """
    try:
        approval_uuid = uuid.UUID(approval_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid approval_id format")

    now = datetime.now(UTC)

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_requests WHERE id = $1",
            [approval_uuid],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")

        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Approval already {row['status']}",
            )

        if row["expires_at"] < now:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Approval request has expired",
            )

        task_uuid = row["task_id"]

        # 更新审批状态
        await conn.execute(
            """
            UPDATE approval_requests
            SET status = 'rejected',
                approver_comment = $1,
                responded_at = $2
            WHERE id = $3
            """,
            [comment, now, approval_uuid],
        )

        # 标记所有 steps 为 dead
        await conn.execute(
            """
            UPDATE task_steps
            SET state = 'dead', updated_at = $1
            WHERE task_id = $2 AND state IN ('pending', 'running', 'awaiting_approval')
            """,
            [now, task_uuid],
        )

        # 任务失败
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', current_state = 'FAILED', updated_at = $1
            WHERE id = $2
            """,
            [now, task_uuid],
        )

        # 记录
        await conn.execute(
            """
            INSERT INTO task_steer_events (id, task_id, message)
            VALUES ($1, $2, $3)
            """,
            [uuid.uuid4(), task_uuid, f"Rejected ({approval_id}): {comment or 'no comment'}"],
        )

        await conn.commit()

    logger.warning(
        "approval_rejected_via_api",
        approval_id=approval_id,
        task_id=str(task_uuid),
        comment=comment,
    )

    return ApprovalActionResponse(
        approval_id=approval_id,
        action="reject",
        new_status="rejected",
        responded_at=now.isoformat(),
    )
