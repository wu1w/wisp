"""
Agent 集成测试（需要 PostgreSQL）。

测试 WispAgent 与 LLMGateway、ToolRegistry 的完整交互。
使用 MockLLMProvider 避免真实 LLM 调用。
"""

from __future__ import annotations

import uuid
from typing import Any

import asyncpg
import pytest

from src.core.agent import WispAgent
from src.core.llm.base import BaseLLMProvider
from src.core.llm.factory import LLMFactory
from src.models.schemas import LLMMessage, LLMResponse


class MockLLMProvider(BaseLLMProvider):
    """Mock LLM Provider：用于测试环境，不发起真实 HTTP 请求。"""

    def __init__(self, responses: list[LLMResponse] | LLMResponse) -> None:
        self._responses = responses if isinstance(responses, list) else [responses]
        self._idx = 0

    @property
    def supports_function_calling(self) -> bool:
        return True

    def get_token_count(self, text: str) -> int:
        return len(text) // 4

    async def chat_completion(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        model: str | None = None,
    ) -> LLMResponse:
        resp = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        return resp


class TestWispAgent:
    """WispAgent 状态机测试。"""

    @pytest.mark.asyncio
    async def test_agent_run_with_text_response(
        self,
        db_conn: asyncpg.Connection[asyncpg.Record],
    ):
        """
        场景：LLM 直接返回文本（无 tool_calls）→ Agent 立即 DONE。
        """
        task_id = await self._create_task(db_conn, "Say hello in one word")

        # 注册 mock provider
        LLMFactory.register("mock", MockLLMProvider)
        mock_resp = LLMResponse(
            content="Hello!",
            tool_calls=None,
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            model="mock",
            provider="mock",
        )

        # 替换 gateway 中的 provider
        agent = WispAgent(task_id, profile="coding")
        agent._gateway._providers["mock"] = MockLLMProvider(mock_resp)

        # 修改 gateway 使用 mock
        agent._gateway._config = {
            "profiles": {
                "coding": {
                    "provider": "mock",
                    "model": "mock",
                    "temperature": 0.0,
                    "max_tokens": 100,
                }
            }
        }

        result = await agent.run()
        assert result["state"] == "DONE"
        assert "Hello" in result["content"]

    @pytest.mark.asyncio
    async def test_agent_run_with_tool_call(
        self,
        db_conn: asyncpg.Connection[asyncpg.Record],
    ):
        """
        场景：LLM 返回 tool_call（read_file），Agent 执行工具，返回结果给 LLM，
              LLM 再返回文本 → Agent DONE。
        """
        task_id = await self._create_task(
            db_conn,
            "Read the file /tmp/wisp/test.txt and tell me its content",
        )

        # 准备测试文件
        test_file = "/tmp/wisp/test.txt"
        content = "Hello from integration test!"
        with open(test_file, "w") as f:
            f.write(content)

        try:
            # Mock: 第一次返回 tool_call，第二次返回文本
            tool_call_response = LLMResponse(
                content=None,
                tool_calls=[
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": f'{{"path": "{test_file}"}}',
                        },
                    }
                ],
                usage={"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
                model="mock",
                provider="mock",
            )
            final_response = LLMResponse(
                content=f"I read the file. Content: {content!r}",
                tool_calls=None,
                usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
                model="mock",
                provider="mock",
            )

            LLMFactory.register("mock", MockLLMProvider)
            agent = WispAgent(task_id, profile="coding")
            agent._gateway._providers["mock"] = MockLLMProvider(
                [tool_call_response, final_response]
            )
            agent._gateway._config = {
                "profiles": {
                    "coding": {
                        "provider": "mock",
                        "model": "mock",
                        "temperature": 0.0,
                        "max_tokens": 200,
                    }
                }
            }

            result = await agent.run()
            assert result["state"] == "DONE"
            assert "integration test" in result["content"]
            assert result["tool_call_count"] == 1

        finally:
            import os
            os.remove(test_file)

    @pytest.mark.asyncio
    async def test_agent_respects_max_tool_calls(
        self,
        db_conn: asyncpg.Connection[asyncpg.Record],
    ):
        """
        场景：LLM 一直返回 tool_calls，到达 max_tool_calls 后 Agent FAILED。
        """
        task_id = await self._create_task(
            db_conn,
            "Keep calling tools forever",
            max_tool_calls=3,
        )

        recursive_response = LLMResponse(
            content=None,
            tool_calls=[
                {
                    "id": "call_recur",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command": "echo loop"}',
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model="mock",
            provider="mock",
        )

        LLMFactory.register("mock", MockLLMProvider)
        agent = WispAgent(task_id, profile="coding")
        agent._gateway._providers["mock"] = MockLLMProvider(recursive_response)
        agent._gateway._config = {
            "profiles": {
                "coding": {
                    "provider": "mock",
                    "model": "mock",
                    "temperature": 0.0,
                    "max_tokens": 100,
                }
            }
        }

        result = await agent.run()
        assert result["state"] == "FAILED"
        assert "Max tool calls" in result["error"]
        assert result["tool_call_count"] == 3

    @pytest.mark.asyncio
    async def test_agent_checkpoint_saved(
        self,
        db_conn: asyncpg.Connection[asyncpg.Record],
    ):
        """
        场景：Agent run 完成后，检查点被正确保存。
        """
        task_id = await self._create_task(db_conn, "Simple reply")

        mock_resp = LLMResponse(
            content="Done!",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            model="mock",
            provider="mock",
        )

        LLMFactory.register("mock", MockLLMProvider)
        agent = WispAgent(task_id, profile="coding")
        agent._gateway._providers["mock"] = MockLLMProvider(mock_resp)
        agent._gateway._config = {
            "profiles": {
                "coding": {
                    "provider": "mock",
                    "model": "mock",
                    "temperature": 0.0,
                    "max_tokens": 50,
                }
            }
        }

        await agent.run()

        # 验证 checkpoint 存在
        row = await db_conn.fetchrow(
            """
            SELECT * FROM agent_checkpoints
            WHERE task_id = $1 AND is_active = true
            """,
            uuid.UUID(task_id),
        )
        assert row is not None
        assert row["step_seq"] == 0
        assert row["messages"]  # 有消息记录

    # ── Helper ─────────────────────────────────────────────

    async def _create_task(
        self,
        conn: asyncpg.Connection[asyncpg.Record],
        description: str,
        user_id: str = "test-user",
        max_tool_calls: int = 50,
    ) -> str:
        """在 DB 中创建一个测试任务，返回 task_id。"""
        task_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO tasks (id, user_id, description, status, current_state,
                               max_tool_calls, tool_call_count)
            VALUES ($1, $2, $3, 'running', 'IDLE', $4, 0)
            """,
            task_id, user_id, description, max_tool_calls,
        )
        step_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO task_steps
                (id, task_id, seq, state, tool_name, input_args, attempt, max_attempts, ttl_seconds)
            VALUES ($1, $2, 0, 'pending', NULL, '{}', 1, 3, 300)
            """,
            step_id, task_id,
        )
        return str(task_id)
