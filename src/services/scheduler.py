"""
任务调度与 Watchdog。

职责：
- 接收任务请求 → 入队（PG task_steps）
- 分配 Worker
- 看门狗（超时检测）
- 死信队列处理
- Hibernate / Wake 状态管理
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from src.db import acquire
from src.utils.config import get_config

logger = structlog.get_logger(__name__)


class SchedulerService:
    """任务调度器。"""

    def __init__(self) -> None:
        self._config = get_config()
        self._worker_cfg = self._config["worker"]
        self._running_tasks: dict[str, Any] = {}

    # ── 任务 CRUD ────────────────────────────────────────────────

    async def submit_task(
        self,
        description: str,
        user_id: str,
        max_tool_calls: int = 50,
    ) -> str:
        """
        提交新任务。

        流程：
        1. 创建 task 记录
        2. 创建初始 Step（IDLE 状态）
        3. 返回 task_id
        """
        task_id = uuid.uuid4()
        step_id = uuid.uuid4()

        async with acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO tasks (id, user_id, description, status, current_state,
                                       max_tool_calls, tool_call_count)
                    VALUES ($1, $2, $3, 'running', 'IDLE', $4, 0)
                    """,
                    task_id, user_id, description, max_tool_calls,
                )
                await conn.execute(
                    """
                    INSERT INTO task_steps
                        (id, task_id, seq, state, tool_name, input_args, attempt, max_attempts, ttl_seconds)
                    VALUES ($1, $2, 0, 'pending', NULL, '{}', 1, $3, $4)
                    """,
                    step_id,
                    task_id,
                    self._worker_cfg["max_attempts"],
                    self._worker_cfg["step_ttl_seconds"],
                )

        # 通知 Worker 有新任务（发布到 Redis Streams）
        try:
            from src.services.redis_streams import stream_queue
            await stream_queue.publish_step(
                step_id=str(step_id),
                task_id=str(task_id),
                seq=0,
            )
        except Exception:
            pass  # Redis 不可用时，Worker 会通过 DB 轮询发现任务

        logger.info("task_submitted", task_id=str(task_id), user_id=user_id)
        return str(task_id)

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        """查询任务当前状态。"""
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1",
                uuid.UUID(task_id),
            )
            if row is None:
                raise ValueError(f"Task not found: {task_id}")

            return dict(row)

    async def list_pending_steps(self, limit: int = 10) -> list[dict[str, Any]]:
        """列出所有待执行的 Step（用于 Worker 抢单）。"""
        async with acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ts.*, t.user_id
                FROM task_steps ts
                JOIN tasks t ON t.id = ts.task_id
                WHERE ts.state = 'pending'
                ORDER BY ts.created_at ASC
                LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    # ── Watchdog ─────────────────────────────────────────────────

    async def watchdog_tick(self) -> None:
        """
        看门狗心跳：检测超时 Step，触发重试或移入死信队列。

        建议每 30 秒执行一次。
        """
        ttl_seconds = self._worker_cfg["step_ttl_seconds"]

        async with acquire() as conn:
            # 查找心跳超时（超过 ttl_seconds 未更新的 running Step）
            stale_rows = await conn.fetch(
                f"""
                SELECT id, task_id, seq, attempt
                FROM task_steps
                WHERE state = 'running'
                  AND heartbeat_at < NOW() - INTERVAL '{ttl_seconds} seconds'
                LIMIT 20
                """,
            )

            for row in stale_rows:
                step_id = row["id"]
                task_id = row["task_id"]
                attempt = row["attempt"]

                if attempt < self._worker_cfg["max_attempts"]:
                    # 重试：重置为 pending，下次被认领
                    await conn.execute(
                        """
                        UPDATE task_steps
                        SET state = 'pending',
                            heartbeat_at = NULL,
                            attempt = attempt + 1
                        WHERE id = $1
                        """,
                        step_id,
                    )
                    logger.warning(
                        "step_heartbeat_timeout_retried",
                        step_id=str(step_id),
                        task_id=str(task_id),
                        attempt=attempt + 1,
                    )
                else:
                    # 超过最大重试次数，移入死信队列
                    await conn.execute(
                        """
                        UPDATE task_steps
                        SET state = 'dead'
                        WHERE id = $1
                        """,
                        step_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO dead_letter_queue
                            (id, task_id, step_id, attempt_count)
                        VALUES ($1, $2, $3, $4)
                        """,
                        uuid.uuid4(),
                        task_id,
                        step_id,
                        attempt,
                    )
                    logger.error(
                        "step_moved_to_dlq",
                        step_id=str(step_id),
                        task_id=str(task_id),
                        attempt=attempt,
                    )


    # ── Hibernate / Wake ───────────────────────────────────────

    async def hibernate_idle_tasks(self) -> int:
        """
        将长时间无活动的 running 任务标记为 HIBERNATING。
        触发条件：running 任务超过 HIBERNATE_AFTER_SECONDS 秒无心跳。
        返回：被标记为 HIBERNATING 的任务数。
        """
        hibernate_after = self._worker_cfg.get("hibernate_after_seconds", 1800)  # 30 min
        async with acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT t.id, t.current_state, t.updated_at
                FROM tasks t
                WHERE t.status = 'running'
                  AND t.current_state NOT IN ('HIBERNATING', 'DONE', 'FAILED')
                  AND t.updated_at < NOW() - INTERVAL '{hibernate_after} seconds'
                LIMIT 20
                """,
            )

            count = 0
            for row in rows:
                task_id = row["id"]
                await conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'running',
                        current_state = 'HIBERNATING',
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    task_id,
                )
                count += 1
                logger.info(
                    "task_hibernated",
                    task_id=str(task_id),
                    last_update=str(row["updated_at"]),
                )

            return count

    async def wake_task(self, task_id: str) -> None:
        """
        唤醒休眠的任务（接收用户新输入时调用）。
        将任务状态从 HIBERNATING 恢复为 IDLE，重新入队。
        """
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, current_state FROM tasks WHERE id = $1",
                uuid.UUID(task_id),
            )
            if row is None:
                return

            if row["current_state"] != "HIBERNATING":
                logger.debug("wake_task_not_hibernating", task_id=task_id)
                return

            await conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    current_state = 'IDLE',
                    updated_at = NOW()
                WHERE id = $1
                """,
                uuid.UUID(task_id),
            )

            # 创建新的 Step 入队
            step_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO task_steps
                    (id, task_id, seq, state, tool_name, attempt, max_attempts, ttl_seconds)
                VALUES ($1, $2,
                    (SELECT COALESCE(MAX(ts.seq), -1) + 1 FROM task_steps ts WHERE ts.task_id = $2),
                    'pending', NULL, 1, $3, $4)
                """,
                step_id,
                uuid.UUID(task_id),
                self._worker_cfg["max_attempts"],
                self._worker_cfg["step_ttl_seconds"],
            )


            # 发布到 Redis Streams（如果可用）
            try:
                from src.services.redis_streams import stream_queue

                await stream_queue.publish_step(
                    step_id=str(step_id),
                    task_id=task_id,
                    seq=0,
                )
            except Exception:
                pass  # Redis 不可用不影响 wake

            logger.info("task_woken", task_id=task_id)

    # ── Step 生命周期 ────────────────────────────────────────────

    async def claim_step(self, worker_id: str) -> dict[str, Any] | None:
        """
        Worker 抢单：认领一个 pending Step。
        使用 SELECT ... FOR UPDATE SKIP LOCKED 保证原子性。
        """
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, task_id, seq, tool_name, input_args, ttl_seconds
                FROM task_steps
                WHERE state = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
            )
            if row is None:
                return None

            step_id = row["id"]
            await conn.execute(
                """
                UPDATE task_steps
                SET state = 'running', heartbeat_at = NOW()
                WHERE id = $1
                """,
                step_id,
            )

            step_dict = dict(row)
            step_dict["worker_id"] = worker_id
            logger.info("step_claimed", step_id=str(step_id), worker_id=worker_id)
            return step_dict

    async def claim_step_by_id(
        self, step_id: str, worker_id: str
    ) -> dict[str, Any] | None:
        """
        根据 step_id 认领指定 Step（用于 Redis Streams 路径）。
        如果 step 已被其他 Worker 认领，返回 None。
        """
        async with acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, task_id, seq, tool_name, input_args, ttl_seconds, state
                FROM task_steps
                WHERE id = $1
                FOR UPDATE SKIP LOCKED
                """,
                uuid.UUID(step_id),
            )
            if row is None:
                return None

            if row["state"] != "pending":
                # 已被认领或已完成
                return None

            await conn.execute(
                """
                UPDATE task_steps
                SET state = 'running', heartbeat_at = NOW()
                WHERE id = $1
                """,
                uuid.UUID(step_id),
            )

            step_dict = dict(row)
            step_dict["worker_id"] = worker_id
            logger.info(
                "step_claimed_by_id",
                step_id=step_id,
                worker_id=worker_id,
            )
            return step_dict

    async def send_heartbeat(self, step_id: str) -> None:
        """刷新 Step 心跳。"""
        async with acquire() as conn:
            await conn.execute(
                "UPDATE task_steps SET heartbeat_at = NOW() WHERE id = $1",
                uuid.UUID(step_id),
            )

    async def complete_step(
        self,
        step_id: str,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        next_state: str | None = None,
        tool_call_count: int = 0,
    ) -> None:
        """
        Step 执行完成。
        - 成功：state → 'done'，产出写入 output
        - 失败：state → 'failed'，错误写入 error
        - next_state：可选，用于设置 Agent 下一状态
        """
        import json

        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE task_steps
                SET state = CASE WHEN $2::text IS NOT NULL THEN 'failed' ELSE 'done' END,
                    output = $3::jsonb,
                    error = $4,
                    updated_at = NOW()
                WHERE id = $1
                """,
                uuid.UUID(step_id),
                error,
                json.dumps(output) if output is not None else None,
                error,
            )

            # 查询关联 task 及其剩余 pending steps
            step_row = await conn.fetchrow(
                "SELECT task_id FROM task_steps WHERE id = $1",
                uuid.UUID(step_id),
            )
            if step_row:
                task_id = str(step_row["task_id"])
                pending_rows = await conn.fetch(
                    "SELECT id FROM task_steps WHERE task_id = $1 AND state = 'pending'",
                    uuid.UUID(task_id),
                )
                has_more = len(pending_rows) > 0

                if error:
                    # Step 失败 → task 直接失败
                    await conn.execute(
                        "UPDATE tasks SET status = 'failed', current_state = 'FAILED', tool_call_count = $1, updated_at = NOW() WHERE id = $2",
                        tool_call_count,
                        uuid.UUID(task_id),
                    )
                elif next_state in ("DONE", "FAILED", "PANIC"):
                    # Agent 达到终止状态 → 检查是否还有未完成 step
                    new_status = "done" if not has_more else "running"
                    await conn.execute(
                        "UPDATE tasks SET status = $1, current_state = $2, tool_call_count = $3, updated_at = NOW() WHERE id = $4",
                        new_status,
                        next_state,
                        tool_call_count,
                        uuid.UUID(task_id),
                    )
                elif next_state:
                    # 中间状态（如 TOOL_CALLING）→ 只更新 current_state，保持 running
                    await conn.execute(
                        "UPDATE tasks SET current_state = $1, tool_call_count = $2, updated_at = NOW() WHERE id = $3",
                        next_state,
                        tool_call_count,
                        uuid.UUID(task_id),
                    )



# 全局单例
scheduler_service = SchedulerService()
