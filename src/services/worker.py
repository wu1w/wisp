"""
任务执行 Worker。

职责：
- 从 Redis Streams 队列取 Step（有任务时立即推送，零轮询）
- 汇报心跳（heartbeat_at）
- 调用 Agent Core 执行
- 执行完毕 → 结果回写 PG
- 检测沙箱退出，自动归档产出物到对象存储
- Hibernate/Wake 协议：30 分钟无活动自动休眠
"""

import asyncio
import signal
import uuid
from typing import Any

import structlog

from src.services.memory import MemoryType, memory_service
from src.services.sandbox import sandbox_service
from src.services.scheduler import scheduler_service
from src.utils.config import get_config

logger = structlog.get_logger(__name__)


class WorkerService:
    """
    Worker 执行器。

    主循环：
      1. claim_step() 抢单（SELECT FOR UPDATE SKIP LOCKED）
      2. 定期 send_heartbeat()
      3. 调用 Agent Core 执行
      4. complete_step() 回写结果
      5. 循环
    """

    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or uuid.uuid4().hex[:8]
        self._config = get_config()
        self._running = False
        self._active_step_id: str | None = None

    # ── 生命周期 ────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 Worker 主循环。"""
        self._running = True
        logger.info("worker_started", worker_id=self.worker_id)

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("worker_tick_error", worker_id=self.worker_id, error=str(exc))
                await asyncio.sleep(5)  # 出错稍后重试

    def stop(self) -> None:
        """优雅停止 Worker。"""
        self._running = False
        logger.info("worker_stopping", worker_id=self.worker_id)

    # ── 主循环 ──────────────────────────────────────────────────

    async def _tick(self) -> None:
        """
        一次主循环迭代。

        优先使用 Redis Streams（高效推送）。
        Redis 不可用时降级为 DB 轮询。
        收到哨兵 None 时执行 Hibernate 检查。
        """
        # 尝试 Redis Streams（高效路径）
        try:
            from src.services.redis_streams import stream_queue

            async for message in stream_queue.consume(self.worker_id):
                # 哨兵：Redis 无新任务，执行 Hibernate 检查后继续监听
                if message is None:
                    await self._check_hibernate()
                    continue

                # 收到任务，认领并执行
                step_id = message["step_id"]
                task_id = message["task_id"]
                seq = message["seq"]
                msg_id = message["id"]

                # 从 DB 查询完整 step 信息（用于执行）
                step = await scheduler_service.claim_step_by_id(step_id, self.worker_id)
                if step is None:
                    # 已被其他 Worker 认领，跳过
                    await stream_queue.ack(msg_id)
                    continue

                self._active_step_id = str(step["id"])
                logger.info(
                    "worker_step_claimed_via_stream",
                    worker_id=self.worker_id,
                    step_id=self._active_step_id,
                    task_id=task_id,
                    seq=seq,
                )

                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(self._active_step_id)
                )
                try:
                    result = await self._execute_step(step)
                    await scheduler_service.complete_step(
                        step_id=self._active_step_id,
                        output=result.get("output"),
                        next_state=result.get("next_state"),
                        tool_call_count=result.get("tool_call_count", 0),
                    )
                    await self._record_evolution_outcome(step, result)
                    logger.info(
                        "worker_step_completed",
                        worker_id=self.worker_id,
                        step_id=self._active_step_id,
                        exit_code=result.get("exit_code"),
                    )
                except Exception as exc:
                    logger.exception(
                        "worker_step_failed",
                        worker_id=self.worker_id,
                        step_id=self._active_step_id,
                        error=str(exc),
                    )
                    await scheduler_service.complete_step(
                        step_id=self._active_step_id,
                        error=str(exc),
                        tool_call_count=0,
                    )
                    await self._record_evolution_outcome(step, {"error": str(exc)})
                finally:
                    heartbeat_task.cancel()
                    self._active_step_id = None
                    await stream_queue.ack(msg_id)

                # 处理完一个任务后立即继续监听，不等待
                return

        except Exception as exc:
            logger.warning(
                "redis_streams_unavailable_falling_back_to_db",
                error=str(exc),
            )
            # Redis 不可用，降级到 DB 轮询
            await self._tick_db_polling()

    async def _tick_db_polling(self) -> None:
        """DB 轮询降级路径（Redis 不可用时使用）。"""
        step = await scheduler_service.claim_step(self.worker_id)
        if step is None:
            await self._check_hibernate()
            await asyncio.sleep(1)
            return

        self._active_step_id = str(step["id"])
        task_id = str(step["task_id"])
        seq = step["seq"]

        logger.info(
            "worker_step_claimed_db",
            worker_id=self.worker_id,
            step_id=self._active_step_id,
            task_id=task_id,
            seq=seq,
        )

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(self._active_step_id))

        try:
            result = await self._execute_step(step)
            await scheduler_service.complete_step(
                step_id=self._active_step_id,
                output=result.get("output"),
                next_state=result.get("next_state"),
                tool_call_count=result.get("tool_call_count", 0),
            )
            await self._record_evolution_outcome(step, result)
            logger.info(
                "worker_step_completed",
                worker_id=self.worker_id,
                step_id=self._active_step_id,
                exit_code=result.get("exit_code"),
            )

        except Exception as exc:
            logger.exception(
                "worker_step_failed",
                worker_id=self.worker_id,
                step_id=self._active_step_id,
                error=str(exc),
            )
            await scheduler_service.complete_step(
                step_id=self._active_step_id,
                error=str(exc),
                tool_call_count=0,
            )
            await self._record_evolution_outcome(step, {"error": str(exc)})

        finally:
            heartbeat_task.cancel()
            self._active_step_id = None

    async def _check_hibernate(self) -> None:
        """
        Hibernate 检查：若任务超过 30 分钟无活动，标记为 HIBERNATING。

        由 Watchdog 调用，或 Worker 空闲时触发。
        """
        try:
            count = await scheduler_service.hibernate_idle_tasks()
            if count > 0:
                logger.info("hibernate_check", tasks_hibernated=count)
        except Exception as exc:
            logger.debug("hibernate_check_skipped", reason=str(exc))

    async def _execute_step(self, step: dict[str, Any]) -> dict[str, Any]:
        """
        执行单个 Step。

        流程：
        1. 从 step 中提取 tool_name 和 input_args
        2. 调用对应的工具处理器
        3. 若涉及沙箱执行，通过 sandbox_service
        4. 若涉及记忆读写，通过 memory_service
        """
        task_id = str(step["task_id"])
        tool_name = step.get("tool_name")
        input_args = step.get("input_args") or {}

        if tool_name is None:
            # 初始化 Step：Agent 进入 THINKING 状态
            return await self._run_agent_step(task_id, step["seq"])

        # 根据工具名路由
        if tool_name == "execute_in_sandbox":
            return await self._handle_sandbox(task_id, input_args)
        elif tool_name == "save_memory":
            return await self._handle_save_memory(task_id, input_args)
        elif tool_name == "search_memory":
            return await self._handle_search_memory(input_args)
        elif tool_name == "reflect_on_error":
            return await self._handle_reflect(task_id, input_args)
        else:
            # 通用工具路由（通过 ToolRegistry）
            return await self._call_tool(tool_name, input_args, task_id)

    async def _run_agent_step(self, task_id: str, seq: int) -> dict[str, Any]:
        """
        运行 Agent 状态机的一个步骤。

        由 Agent Core 接管，执行 LLM 推理 + Function Calling。
        """
        # 导入延迟避免循环依赖
        from src.core.agent import WispAgent

        agent = WispAgent(task_id)
        try:
            result = await agent.run()
            return {
                "output": result,
                "next_state": result.get("state", "DONE"),
                "exit_code": 0,
            }
        except Exception as exc:
            logger.exception("agent_run_error", task_id=task_id, error=str(exc))
            return {
                "output": None,
                "next_state": "FAILED",
                "exit_code": -1,
                "error": str(exc),
            }

    # ── 工具处理器 ──────────────────────────────────────────────

    async def _handle_sandbox(self, task_id: str, input_args: dict[str, Any]) -> dict[str, Any]:
        """处理沙箱执行工具调用。"""
        command = input_args.get("command", "")
        lang = input_args.get("lang", "bash")
        timeout = min(input_args.get("timeout", 60), 300)

        sandbox_result = await sandbox_service.execute(
            command=command,
            lang=lang,
            timeout=timeout,
            workdir=input_args.get("workdir", "/workspace"),
        )

        # 沙箱执行完毕，触发 ETL Pipeline（记忆保存）
        _ = asyncio.create_task(
            memory_service.save(
                type=MemoryType.EPISODIC,
                content=f"[Sandbox {lang}] exit={sandbox_result['exit_code']}\n"
                        f"stdout={sandbox_result['stdout'][:500]}\n"
                        f"stderr={sandbox_result['stderr'][:200]}",
                metadata={
                    "exit_code": sandbox_result["exit_code"],
                    "duration_ms": sandbox_result["duration_ms"],
                    "killed": sandbox_result["killed"],
                    "lang": lang,
                },
                task_id=task_id,
                tool_name="execute_in_sandbox",
                success=(sandbox_result["exit_code"] == 0),
            )
        )

        return {
            "output": sandbox_result,
            "next_state": "TOOL_CALLING",
            "exit_code": sandbox_result["exit_code"],
        }

    async def _handle_save_memory(
        self,
        task_id: str,
        input_args: dict[str, Any],
    ) -> dict[str, Any]:
        """处理保存记忆。"""
        memory_id = await memory_service.save(
            type=MemoryType(input_args.get("type", "episodic")),
            content=input_args.get("content", ""),
            metadata=input_args.get("metadata"),
            task_id=task_id,
            tool_name="save_memory",
            success=input_args.get("success"),
        )
        return {
            "output": {"memory_id": memory_id},
            "next_state": "TOOL_CALLING",
            "exit_code": 0,
        }

    async def _handle_search_memory(
        self,
        input_args: dict[str, Any],
    ) -> dict[str, Any]:
        """处理记忆检索。"""
        results = await memory_service.search(
            query=input_args.get("query", ""),
            memory_type=input_args.get("memory_type", "all"),
            task_id=input_args.get("task_id"),
            top_k=input_args.get("top_k", 5),
        )
        return {
            "output": {"results": results},
            "next_state": "TOOL_CALLING",
            "exit_code": 0,
        }

    async def _handle_reflect(
        self,
        task_id: str,
        input_args: dict[str, Any],
    ) -> dict[str, Any]:
        """处理反思工具（reflect_on_error）。"""
        error_message = input_args.get("error_message", "")
        exit_code = input_args.get("exit_code", -1)
        failed_command = input_args.get("failed_command", "")
        attempt = input_args.get("attempt", 1)

        # 调用 LLM 进行反思分析（此处简化，实际由 Agent Core 接管）
        reflection_content = (
            f"[Reflection] Error on attempt {attempt}: {error_message}\n"
            f"Failed command: {failed_command}\n"
            f"Exit code: {exit_code}"
        )

        # 保存反思记忆
        memory_id = await memory_service.save(
            type=MemoryType.REFLECTIVE,
            content=reflection_content,
            metadata={
                "exit_code": exit_code,
                "attempt": attempt,
                "task_id": task_id,
            },
            task_id=task_id,
            tool_name="reflect_on_error",
            success=False,
        )

        return {
            "output": {
                "memory_id": memory_id,
                "analysis": reflection_content,
                "should_retry": attempt < 3,
            },
            "next_state": "TOOL_CALLING",
            "exit_code": 0,
        }

    async def _call_tool(
        self,
        tool_name: str,
        input_args: dict[str, Any],
        task_id: str,
    ) -> dict[str, Any]:
        """通用工具调用（通过 ToolRegistry）。"""
        from src.core.tools import registry

        try:
            result = await registry.call(tool_name, **input_args)
            return {
                "output": result,
                "next_state": "TOOL_CALLING",
                "exit_code": 0,
            }
        except Exception as exc:
            logger.exception("tool_call_error", tool=tool_name, error=str(exc))
            return {
                "output": None,
                "next_state": "TOOL_CALLING",
                "exit_code": -1,
                "error": str(exc),
            }

    # ── 心跳 ────────────────────────────────────────────────────

    async def _heartbeat_loop(self, step_id: str) -> None:
        """定期发送心跳，直到 step 完成或 Worker 停止。"""
        interval = self._config["worker"]["heartbeat_interval_seconds"]

        while self._running and self._active_step_id == step_id:
            await asyncio.sleep(interval)
            if self._active_step_id == step_id:
                try:
                    await scheduler_service.send_heartbeat(step_id)
                    logger.debug("worker_heartbeat", step_id=step_id)
                except Exception as exc:
                    logger.warning("heartbeat_failed", step_id=step_id, error=str(exc))


# ── Worker 入口（可独立运行）───────────────────────────────────


    async def _record_evolution_outcome(
        self,
        step: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """
        将 step 执行结果记录到 Evolution Engine（用于 Prompt 进化分析）。
        仅在 agent step (tool_name is None) 时记录。
        """
        if step.get("tool_name") is not None:
            return

        task_id = str(step["task_id"])
        next_state = result.get("next_state", "")
        is_success = next_state in ("DONE",) and result.get("exit_code", -1) == 0

        tool_calls = result.get("tool_call_count", 0) or 0
        error_count = 1 if result.get("error") else 0

        profile = "chatting"
        try:
            from src.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT description FROM tasks WHERE id = $1",
                    uuid.UUID(task_id),
                )
                if row:
                    desc = row["description"].lower()
                    if any(k in desc for k in ["code", "debug", "git", "refactor", "file", "python", "bash"]):
                        profile = "coding"
                    elif any(k in desc for k in ["cheap", "cost", "budget"]):
                        profile = "cheap"
        except Exception:
            pass

        try:
            from src.services.evolution import evolution_engine
            await evolution_engine.record_outcome(
                task_id=task_id,
                profile=profile,
                success=is_success,
                tool_calls=tool_calls,
                error_count=error_count,
                reflection_summary=(
                    result.get("content", "")[:500]
                    if is_success
                    else result.get("error", "")[:200]
                ),
                latency_seconds=result.get("latency_seconds", 0.0),
            )
        except Exception as exc:
            logger.warning("evolution_record_outcome_failed", error=str(exc))


async def main() -> None:
    """Worker 进程入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="Wisp Worker")
    parser.add_argument("--worker-id", help="Worker ID（默认自动生成）")
    args = parser.parse_args()

    # 初始化 DB 连接池
    from src.db import close_pool, init_pool
    await init_pool()

    worker = WorkerService(worker_id=args.worker_id)

    # 优雅退出信号处理
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.start()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
