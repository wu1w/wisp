"""
Wisp Agent 状态机主循环。

禁止在本文 件中直接 import openai / anthropic 等 SDK。
所有 LLM 调用必须通过 src.core.llm.gateway.LLMGateway。

状态流转：
  IDLE → THINKING → TOOL_CALLING → (循环)
            ↓                    ↓
        DONE 或 FAILED      AWAITING_APPROVAL
                                  ↓
                              TOOL_CALLING
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog

from src.core.llm.gateway import LLMGateway
from src.core.prompts import prompt_manager
from src.core.tools import registry
from src.db import acquire
from src.models.schemas import LLMMessage
from src.utils.config import get_config

logger = structlog.get_logger(__name__)


class AgentState:
    """Agent 状态常量。"""

    IDLE = "IDLE"
    THINKING = "THINKING"
    TOOL_CALLING = "TOOL_CALLING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    HIBERNATING = "HIBERNATING"
    DONE = "DONE"
    FAILED = "FAILED"
    PANIC = "PANIC"  # Circuit Breaker 触发


# ── Circuit Breaker ────────────────────────────────────────────

@dataclass
class _StepRecord:
    """单步执行记录（用于死循环检测）。"""

    tool_name: str
    output_hash: str
    seq: int


class CircuitBreaker:
    """
    死循环检测器。

    检测规则：同一个工具连续执行 ≥3 次，且输出完全相同 → 判定为死循环。
    触发 Circuit Breaker → Agent 进入 PANIC 状态，通知用户。
    """

    HISTORY_SIZE = 10  # 保留最近 N 条历史

    def __init__(self) -> None:
        self._history: dict[str, list[_StepRecord]] = {}  # task_id → 历史

    def record(self, task_id: str, tool_name: str, output: Any, seq: int) -> None:
        """记录一步执行。"""
        if task_id not in self._history:
            self._history[task_id] = []

        output_str = json.dumps(output, sort_keys=True, ensure_ascii=False)
        output_hash = hashlib.sha256(output_str.encode()).hexdigest()[:16]

        self._history[task_id].append(_StepRecord(
            tool_name=tool_name,
            output_hash=output_hash,
            seq=seq,
        ))
        # 保留最近 HISTORY_SIZE 条
        if len(self._history[task_id]) > self.HISTORY_SIZE:
            self._history[task_id] = self._history[task_id][-self.HISTORY_SIZE:]

        logger.debug(
            "circuit_breaker_record",
            task_id=task_id,
            tool=tool_name,
            hash=output_hash,
            seq=seq,
            history_len=len(self._history[task_id]),
        )

    def is_stuck(self, task_id: str) -> tuple[bool, str]:
        """
        检测是否陷入死循环。

        返回：(is_stuck, reason)
        """
        hist = self._history.get(task_id, [])
        if len(hist) < 3:
            return False, ""

        last_3 = hist[-3:]
        if all(h.tool_name == last_3[0].tool_name for h in last_3):
            if len(set(h.output_hash for h in last_3)) == 1:
                reason = (
                    f"Dead loop detected: {last_3[0].tool_name!r} "
                    f"executed 3 times with identical output (seq={last_3[0].seq})"
                )
                return True, reason

        return False, ""

    def reset(self, task_id: str) -> None:
        """重置某个任务的历史（任务结束后调用）。"""
        self._history.pop(task_id, None)


# 全局单例
circuit_breaker = CircuitBreaker()


# ── Agent ─────────────────────────────────────────────────────

class WispAgent:
    """
    Agent 状态机。

    执行一个完整的任务（从 IDLE 到 DONE/FAILED）。
    严禁直接调用 LLM SDK，只通过 LLMGateway。
    """

    def __init__(self, task_id: str, profile: str = "coding") -> None:
        self.task_id = task_id
        self.profile = profile
        self.state: str = AgentState.IDLE
        self.tool_call_count: int = 0
        self._gateway = LLMGateway()
        self._config = get_config()
        self._max_tool_calls = self._config["agent"]["max_tool_calls"]

        # 上下文（从 Checkpoint 恢复）
        self._messages: list[dict[str, Any]] = []
        self._core_facts: dict[str, Any] = {}
        self._variable_context: dict[str, Any] = {}
        self._pending_steps: list[dict[str, Any]] = []

        # 加载 Profile 对应的 System Prompt
        self._system_prompt = prompt_manager.get_active_prompt(profile)

    async def run(self) -> dict[str, Any]:
        """
        执行完整的 Agent 任务。

        主循环：
          1. 构建消息（含 System Prompt + History）
          2. 调用 LLMGateway.chat()（带工具列表）
          3. 解析响应（content 或 tool_calls）
          4. 若为 tool_calls：执行工具 → 保存结果 → 追加消息 → 循环
          5. 若为 content：任务完成
        """
        logger.info("agent_run_start", task_id=self.task_id)

        # 初始化消息列表
        self._messages = [
            {"role": "system", "content": self._system_prompt},
        ]

        # 加载任务描述
        task_info = await self._load_task()
        if task_info is None:
            self.state = AgentState.FAILED
            return {"state": AgentState.FAILED, "error": "Task not found"}

        # 任务级 max_tool_calls 优先于全局配置
        task_max = task_info.get("max_tool_calls")
        if task_max is not None and task_max > 0:
            self._max_tool_calls = task_max

        self._messages.append({
            "role": "user",
            "content": task_info["description"],
        })

        # 主循环
        while self.tool_call_count < self._max_tool_calls:
            # ── Circuit Breaker 检测 ──────────────────────
            is_stuck, reason = circuit_breaker.is_stuck(self.task_id)
            if is_stuck:
                logger.error("circuit_breaker_triggered", task_id=self.task_id, reason=reason)
                self.state = AgentState.PANIC
                await self._trigger_panic(reason)
                return {
                    "state": AgentState.PANIC,
                    "error": reason,
                    "tool_call_count": self.tool_call_count,
                }

            if self.state == AgentState.AWAITING_APPROVAL:
                logger.info("agent_awaiting_approval", task_id=self.task_id)
                await self._wait_for_approval()
                continue

            # 调用 LLM（通过 Gateway）
            self.state = AgentState.THINKING

            try:
                llm_messages = [
                    LLMMessage(
                        role=m["role"],
                        content=m.get("content"),
                        tool_calls=m.get("tool_calls"),
                        tool_call_id=m.get("tool_call_id"),
                    )
                    for m in self._messages
                ]

                response = await self._gateway.chat(
                    messages=llm_messages,
                    profile=self.profile,
                    tools=registry.get_all_tools(),
                    tool_choice="auto",
                    temperature=0.0,
                )
            except Exception as exc:
                logger.exception("llm_call_failed", task_id=self.task_id, error=str(exc))
                self.state = AgentState.FAILED
                return {"state": AgentState.FAILED, "error": f"LLM error: {exc}"}

            # 解析 tool_calls
            if response.tool_calls:
                self.state = AgentState.TOOL_CALLING

                for tool_call in response.tool_calls:
                    result = await self._execute_tool_call(tool_call)
                    self.tool_call_count += 1

                    # ── Circuit Breaker 记录 ─────────────
                    func = tool_call.get("function", {})
                    tool_name = func.get("name", "")
                    circuit_breaker.record(
                        self.task_id, tool_name,
                        result.get("result", {}),
                        self.tool_call_count,
                    )

                    if result.get("requires_approval"):
                        self.state = AgentState.AWAITING_APPROVAL
                        await self._save_checkpoint()
                        break

                if self.state == AgentState.AWAITING_APPROVAL:
                    continue

                # 执行完 tool_calls 后，继续循环再次调用 LLM
                continue

            # 无 tool_calls，内容回复 → 任务完成
            final_content = response.content or ""
            self.state = AgentState.DONE

            logger.info(
                "agent_run_complete",
                task_id=self.task_id,
                tool_calls=self.tool_call_count,
                final_content_length=len(final_content),
            )

            await self._save_checkpoint()
            circuit_breaker.reset(self.task_id)
            return {
                "state": AgentState.DONE,
                "content": final_content,
                "tool_call_count": self.tool_call_count,
            }

        # 超过最大 tool_calls 上限
        self.state = AgentState.FAILED
        logger.warning("agent_max_tool_calls_exceeded", task_id=self.task_id)
        circuit_breaker.reset(self.task_id)
        return {
            "state": AgentState.FAILED,
            "error": f"Max tool calls ({self._max_tool_calls}) exceeded",
            "tool_call_count": self.tool_call_count,
        }

    async def _execute_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """
        执行单个工具调用。

        1. 解析 tool_call（id + function）
        2. 调用 registry.call()
        3. 追加 tool 结果消息
        4. 写入 tool_executions 表
        """
        call_id = tool_call["id"]
        func = tool_call.get("function", {})
        name = func.get("name", "")
        raw_args = func.get("arguments", "{}")

        try:
            parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            parsed_args = {}

        logger.info(
            "agent_tool_call",
            task_id=self.task_id,
            tool=name,
            call_id=call_id,
        )

        try:
            result = await registry.call(name, **parsed_args)
            requires_approval = False
        except Exception as exc:
            logger.exception("tool_execution_failed", tool=name, error=str(exc))
            result = {"error": str(exc)}
            requires_approval = False

        # 追加 tool 角色消息
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False),
        })

        # 记录到 tool_executions 表
        await self._record_tool_execution(name, parsed_args, result)

        return {"result": result, "requires_approval": requires_approval}

    async def _load_task(self) -> dict[str, Any] | None:
        """从 PG 加载任务信息。"""
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1",
                uuid.UUID(self.task_id),
            )
            return dict(row) if row else None

    async def _save_checkpoint(self) -> None:
        """保存 Agent 执行快照到 Checkpoint 表。"""
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE agent_checkpoints
                SET is_active = false
                WHERE task_id = $1 AND is_active = true
                """,
                uuid.UUID(self.task_id),
            )

            checkpoint_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO agent_checkpoints
                    (id, task_id, step_seq, workflow_state, messages,
                     core_facts, variable_context, pending_steps,
                     prompt_version, is_active)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, true)
                """,
                checkpoint_id,
                uuid.UUID(self.task_id),
                self.tool_call_count,
                json.dumps({"state": self.state}),
                json.dumps(self._messages),
                json.dumps(self._core_facts),
                json.dumps(self._variable_context),
                json.dumps(self._pending_steps),
                self._config["agent"].get("default_prompt_version", "v2.0"),
            )

        logger.debug("checkpoint_saved", task_id=self.task_id, checkpoint_id=str(checkpoint_id))

    async def _wait_for_approval(self) -> None:
        """暂停执行，等待人工审批。"""
        import asyncio

        while True:
            await asyncio.sleep(3)

            async with acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, current_state FROM tasks WHERE id = $1",
                    uuid.UUID(self.task_id),
                )
                if row is None:
                    return

                if row["status"] in ("failed", "completed"):
                    self.state = AgentState.FAILED if row["status"] == "failed" else AgentState.DONE
                    return

                step_row = await conn.fetchrow(
                    """
                    SELECT state FROM task_steps
                    WHERE task_id = $1 AND state = 'done'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    uuid.UUID(self.task_id),
                )
                if step_row is not None:
                    self.state = AgentState.TOOL_CALLING
                    return

    async def _trigger_panic(self, reason: str) -> None:
        """Circuit Breaker 触发 PANIC：更新任务状态并记录死循环报告。"""
        circuit_breaker.reset(self.task_id)
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE tasks
                SET status = 'failed', current_state = 'PANIC'
                WHERE id = $1
                """,
                uuid.UUID(self.task_id),
            )
        logger.error("agent_panic", task_id=self.task_id, reason=reason)

    async def _record_tool_execution(
        self,
        tool_name: str,
        input_args: dict[str, Any],
        output: Any,
    ) -> None:
        """写入 tool_executions 表。"""
        async with acquire() as conn:
            exec_id = uuid.uuid4()
            task_uuid = uuid.UUID(self.task_id)

            row = await conn.fetchrow(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM tool_executions WHERE task_id = $1",
                task_uuid,
            )
            next_seq = row["next_seq"]

            await conn.execute(
                """
                INSERT INTO tool_executions
                    (id, task_id, seq, tool_name, input_args, output)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
                """,
                exec_id, task_uuid, next_seq, tool_name,
                json.dumps(input_args) if isinstance(input_args, dict) else input_args,
                json.dumps(output) if isinstance(output, dict) else output,
            )
