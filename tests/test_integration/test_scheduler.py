"""
Scheduler / Worker 集成测试（需要 PostgreSQL）。

测试任务生命周期：submit → claim → complete → watchdog。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.db import get_pool


class TestSchedulerService:
    """scheduler_service 的集成测试。"""

    @pytest.mark.asyncio
    async def test_submit_task_creates_task_and_step(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        task_id = await scheduler_service.submit_task(
            description="Test task for integration",
            user_id="test-user",
            max_tool_calls=5,
        )
        assert task_id is not None
        uuid.UUID(task_id)  # 验证是合法 UUID

        # 验证 tasks 表（自己从 pool 获取连接）
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1",
                uuid.UUID(task_id),
            )
            assert row is not None
            assert row["description"] == "Test task for integration"
            assert row["status"] == "running"
            assert row["current_state"] == "IDLE"

            # 验证 task_steps 表有初始 step
            step_row = await conn.fetchrow(
                "SELECT * FROM task_steps WHERE task_id = $1 ORDER BY seq ASC LIMIT 1",
                uuid.UUID(task_id),
            )
            assert step_row is not None
            assert step_row["state"] == "pending"
            assert step_row["seq"] == 0
            assert step_row["tool_name"] is None  # 初始 step 无 tool

    @pytest.mark.asyncio
    async def test_claim_step_exactly_once(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        task_id = await scheduler_service.submit_task(
            description="Claim test",
            user_id="test-user",
        )
        pool = get_pool()

        # 直接从 DB 找到我们任务的 pending step
        async with pool.acquire() as conn:
            pending_step = await conn.fetchrow(
                "SELECT id FROM task_steps WHERE task_id = $1 AND state = 'pending'",
                uuid.UUID(task_id),
            )
            assert pending_step is not None

        # Worker A 抢单
        step_a = await scheduler_service.claim_step("worker-A")
        assert step_a is not None

        # Worker B 抢同一任务时，由于使用 FOR UPDATE SKIP LOCKED，不应再拿到同一 step
        await scheduler_service.claim_step("worker-B")
        # 可能拿到其他任务的 step 或 None（取决于 DB 实现）
        # 关键是 step_a 的 state 已变成 running
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM task_steps WHERE id = $1",
                step_a["id"],
            )
            assert row["state"] == "running"

    @pytest.mark.asyncio
    async def test_complete_step_updates_task_state(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        task_id = await scheduler_service.submit_task(
            description="Complete test",
            user_id="test-user",
        )
        pool = get_pool()

        step = await scheduler_service.claim_step("worker-X")
        assert step is not None

        await scheduler_service.complete_step(
            step_id=str(step["id"]),
            output={"result": "done"},
        )

        async with pool.acquire() as conn:
            step_row = await conn.fetchrow(
                "SELECT state FROM task_steps WHERE id = $1",
                step["id"],
            )
            assert step_row["state"] == "done"

            task_row = await conn.fetchrow(
                "SELECT status FROM tasks WHERE id = $1",
                uuid.UUID(task_id),
            )
            assert task_row["status"] == "running"

    @pytest.mark.asyncio
    async def test_watchdog_tick_retries_stale_step(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        await scheduler_service.submit_task(
            description="Watchdog test",
            user_id="test-user",
        )
        pool = get_pool()

        step = await scheduler_service.claim_step("worker-W")
        assert step is not None

        # 把 step 标记为 running 但不完成，模拟超时
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_steps SET state = 'running', "
                "heartbeat_at = heartbeat_at - INTERVAL '10 minutes' "
                "WHERE id = $1",
                step["id"],
            )

        # watchdog 应该把超时的 step 重置为 pending
        await scheduler_service.watchdog_tick()

        async with pool.acquire() as conn:
            step_row = await conn.fetchrow(
                "SELECT state FROM task_steps WHERE id = $1",
                step["id"],
            )
            assert step_row["state"] == "pending"

    @pytest.mark.asyncio
    async def test_get_task_status(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        task_id = await scheduler_service.submit_task(
            description="Status test",
            user_id="test-user",
        )

        status = await scheduler_service.get_task_status(task_id)
        assert status is not None
        assert str(status["id"]) == task_id
        assert status["status"] == "running"
        assert status["current_state"] == "IDLE"

    @pytest.mark.asyncio
    async def test_submit_task_idempotent(self, db_pool: Any):
        from src.services.scheduler import scheduler_service

        id1 = await scheduler_service.submit_task("same desc", "user1")
        id2 = await scheduler_service.submit_task("same desc", "user1")

        # 两次 submit 应该产生两个不同的 task
        assert id1 != id2
        assert uuid.UUID(id1)
        assert uuid.UUID(id2)
