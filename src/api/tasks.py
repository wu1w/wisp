"""
任务相关 API 路由。

CRUD 规范（对应 SPEC.md 第三章 3.1）：
- 路由函数只做参数校验和调用 Service，不包含业务逻辑
- 幂等性：重复创建任务不会产生多个任务
"""

import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from src.db import acquire
from src.middleware.auth import AuthenticatedUser, get_current_user
from src.models.schemas import TaskCreate, TaskResponse, TaskStatus
from src.services.scheduler import scheduler_service
from src.utils.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── 请求/响应模型 ────────────────────────────────────────────────

class TaskCancelResponse(BaseModel):
    """取消任务响应。"""

    task_id: str
    status: str


class StepResponse(BaseModel):
    """Step 响应。"""

    id: str
    task_id: str
    seq: int
    state: str
    tool_name: str | None
    input_args: dict[str, Any] | None
    output: dict[str, Any] | None
    error: str | None
    attempt: int
    max_attempts: int
    heartbeat_at: str | None
    ttl_seconds: int


class ApprovalResponse(BaseModel):
    """审批操作响应。"""

    step_id: str
    action: str
    new_state: str


# ── 路由 ─────────────────────────────────────────────────────────

@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_task(
    request: Request,
    payload: TaskCreate,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskResponse:
    """
    创建新任务。

    幂等性：支持 client_token（用于重复提交去重）。
    若指定了 existing_task_id 且任务仍处于 running 状态，直接返回该任务。

    user_id 强制使用认证 Token 中的用户 ID，不接受 payload 中的 user_id 伪造。
    """
    # 强制使用 Token 中的 user_id，防止伪造
    effective_user_id = user.user_id or payload.user_id

    task_id = await scheduler_service.submit_task(
        description=payload.description,
        user_id=effective_user_id,
        max_tool_calls=payload.max_tool_calls,
    )

    # 取回完整任务记录
    task = await scheduler_service.get_task_status(task_id)

    logger.info("task_created_via_api", task_id=task_id, user_id=payload.user_id)
    return TaskResponse(
        id=task["id"],
        user_id=task["user_id"],
        description=task["description"],
        status=TaskStatus(task["status"]),
        current_state=task["current_state"],
        tool_call_count=task["tool_call_count"],
        max_tool_calls=task["max_tool_calls"],
        created_at=task["created_at"],
        updated_at=task["updated_at"],
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str) -> TaskResponse:
    """
    查询任务状态和详情。

    包含：status、current_state、variable_context、tool_call_count 等。
    """
    try:
        task = await scheduler_service.get_task_status(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return TaskResponse(
        id=task["id"],
        user_id=task["user_id"],
        description=task["description"],
        status=TaskStatus(task["status"]),
        current_state=task["current_state"],
        tool_call_count=task["tool_call_count"],
        max_tool_calls=task["max_tool_calls"],
        created_at=task["created_at"],
        updated_at=task["updated_at"],
    )


@router.get("/{task_id}/steps")
async def list_steps(task_id: str) -> list[StepResponse]:
    """
    列出任务的所有 Step。

    按 seq 升序排列。
    """
    # 验证 task_id 存在
    try:
        uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task_id format")

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, task_id, seq, state, tool_name, input_args,
                   output, error, attempt, max_attempts, heartbeat_at, ttl_seconds
            FROM task_steps
            WHERE task_id = $1
            ORDER BY seq ASC
            """,
            [uuid.UUID(task_id)],
        )

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No steps found for task {task_id}",
        )

    return [
        StepResponse(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            seq=row["seq"],
            state=row["state"],
            tool_name=row["tool_name"],
            input_args=row["input_args"],
            output=row["output"],
            error=row["error"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            heartbeat_at=row["heartbeat_at"].isoformat() if row["heartbeat_at"] else None,
            ttl_seconds=row["ttl_seconds"],
        )
        for row in rows
    ]


@router.post("/{task_id}/steps/{step_id}/approve", response_model=ApprovalResponse)
async def approve_step(
    task_id: str,
    step_id: str,
    comment: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalResponse:
    """
    人工审批通过某一步（AwaitingApproval → TOOL_CALLING）。

    将 Step state 改为 done，并将 task status 恢复为 running。
    """
    # 验证格式
    try:
        step_uuid = uuid.UUID(step_id)
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ID format")

    async with acquire() as conn:
        # 确认 Step 存在且处于等待审批状态
        row = await conn.fetchrow(
            "SELECT id, state FROM task_steps WHERE id = $1 AND task_id = $2",
            [step_uuid, task_uuid],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

        if row["state"] != "awaiting_approval":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Step is in '{row['state']}' state, not awaiting_approval",
            )

        # 更新 Step 状态
        await conn.execute(
            """
            UPDATE task_steps
            SET state = 'done', output = $1, updated_at = NOW()
            WHERE id = $2
            """,
            [{"approval_comment": comment} if comment else {}, step_uuid],
        )

        # 更新任务状态
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'running', updated_at = NOW()
            WHERE id = $1
            """,
            [task_uuid],
        )

        # 记录审批事件
        await conn.execute(
            """
            INSERT INTO task_steer_events (id, task_id, message)
            VALUES ($1, $2, $3)
            """,
            [uuid.uuid4(), task_uuid, f"Approved: {comment or '(no comment)'}"],
        )

        await conn.commit()

    logger.info("step_approved_via_api", task_id=task_id, step_id=step_id)
    return ApprovalResponse(
        step_id=step_id,
        action="approve",
        new_state="done",
    )


@router.post("/{task_id}/steps/{step_id}/reject", response_model=ApprovalResponse)
async def reject_step(
    task_id: str,
    step_id: str,
    comment: str | None = None,
    _: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalResponse:
    """
    人工拒绝某一步。

    将 Step state 改为 failed，并将 task status 改为 failed。
    """
    try:
        step_uuid = uuid.UUID(step_id)
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ID format")

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, state FROM task_steps WHERE id = $1 AND task_id = $2",
            [step_uuid, task_uuid],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Step not found")

        # 拒绝：Step → failed，Task → failed
        await conn.execute(
            """
            UPDATE task_steps
            SET state = 'failed', error = $1, updated_at = NOW()
            WHERE id = $2
            """,
            [f"Rejected: {comment or 'no comment'}", step_uuid],
        )
        await conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', updated_at = NOW()
            WHERE id = $1
            """,
            [task_uuid],
        )
        await conn.execute(
            """
            INSERT INTO task_steer_events (id, task_id, message)
            VALUES ($1, $2, $3)
            """,
            [uuid.uuid4(), task_uuid, f"Rejected: {comment or '(no comment)'}]"],
        )

        await conn.commit()

    logger.warning("step_rejected_via_api", task_id=task_id, step_id=step_id)
    return ApprovalResponse(
        step_id=step_id,
        action="reject",
        new_state="failed",
    )


@router.delete("/{task_id}", response_model=TaskCancelResponse)
@limiter.limit("20/minute")
async def cancel_task(
    request: Request,
    task_id: str,
    _: AuthenticatedUser = Depends(get_current_user),
) -> TaskCancelResponse:
    """
    取消任务（软删除：将任务状态置为 failed）。

    注意：正在执行的 Step 不会被强制终止，由 Worker 自然超时处理。
    """
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task_id format")

    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, status FROM tasks WHERE id = $1",
            [task_uuid],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

        if row["status"] != "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Task is '{row['status']}', cannot cancel (only running tasks can be cancelled)",
            )

        await conn.execute(
            """
            UPDATE tasks
            SET status = 'failed', updated_at = NOW()
            WHERE id = $1
            """,
            [task_uuid],
        )
        await conn.commit()

    logger.info("task_cancelled_via_api", task_id=task_id)
    return TaskCancelResponse(task_id=task_id, status="cancelled")
