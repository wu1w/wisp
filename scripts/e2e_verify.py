#!/usr/bin/env python3
"""
Wisp Agent 端到端验证脚本。

用法:
    cd /home/ubuntu/wisp
    source .venv/bin/activate
    source .env
    python scripts/e2e_verify.py

验证内容:
    1. DB 连接 + Schema
    2. Redis 连接
    3. Agent 状态机 (MockLLM)
    4. 真实 LLM 调用 (如果 credentials 可用)
"""

import asyncio
import sys
import uuid
from pathlib import Path

# ── Bootstrap ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.schemas import LLMMessage, LLMResponse
from src.core.agent import WispAgent
from src.core.llm.base import BaseLLMProvider
from src.core.llm.factory import LLMFactory
from src.db import init_pool, close_pool, get_pool
from src.services.scheduler import scheduler_service


# ── Mock LLM ────────────────────────────────────────────────────

class MockLLMProvider(BaseLLMProvider):
    """Mock LLM: 返回配置好的固定响应。"""

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
        tools=None,
        tool_choice=None,
        temperature=0.3,
        max_tokens=4096,
        model=None,
    ) -> LLMResponse:
        resp = self._responses[self._idx % len(self._responses)]
        self._idx += 1
        print(f"  [MockLLM] call #{self._idx}, tools={tools is not None}")
        return resp


# ── Tests ───────────────────────────────────────────────────────

async def test_db_connection() -> bool:
    """测试 1: DB 连接 + Schema"""
    print("\n[1/4] DB Connection + Schema...")
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            ver = await conn.fetchval("SELECT version()")
            print(f"  PostgreSQL: {ver.split(',')[0]}")

            tables = await conn.fetch("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name
            """)
            table_names = [r["table_name"] for r in tables]
            print(f"  Tables: {table_names}")
            assert "tasks" in table_names
            assert "task_steps" in table_names
            print("  ✅ DB OK")
            return True
    except Exception as e:
        print(f"  ❌ DB Failed: {e}")
        return False


async def test_redis_connection() -> bool:
    """测试 2: Redis 连接"""
    print("\n[2/4] Redis Connection...")
    try:
        import redis.asyncio as redis
        client = redis.from_url("redis://localhost:6379/0", decode_responses=True)
        pong = await client.ping()
        print(f"  Redis: PONG={pong}")
        await client.aclose()
        print("  ✅ Redis OK")
        return True
    except Exception as e:
        print(f"  ❌ Redis Failed: {e}")
        return False


async def test_agent_mock() -> bool:
    """测试 3: Agent 状态机 (MockLLM)"""
    print("\n[3/4] Agent State Machine (Mock)...")

    pool = get_pool()
    async with pool.acquire() as conn:
        # 创建测试任务
        task_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO tasks (id, user_id, description, status, current_state, max_tool_calls, tool_call_count)
            VALUES ($1, $2, $3, 'running', 'IDLE', 50, 0)
            """,
            task_id, "e2e-test", "Say hello in one word",
        )
        step_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO task_steps (id, task_id, seq, state, tool_name, input_args, attempt, max_attempts, ttl_seconds)
            VALUES ($1, $2, 0, 'pending', NULL, '{}', 1, 3, 300)
            """,
            step_id, task_id,
        )
        task_id_str = str(task_id)
        print(f"  Created task: {task_id_str}")

    try:
        # 注册 mock provider
        LLMFactory.register("mock", MockLLMProvider)

        # 构造 Mock Agent
        mock_resp = LLMResponse(
            content="Hello from mock!",
            tool_calls=None,
            usage={"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            model="mock",
            provider="mock",
        )

        agent = WispAgent(task_id_str, profile="coding")
        # 直接替换 gateway 中的 provider
        agent._gateway._providers["mock"] = MockLLMProvider(mock_resp)
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
        state = result["state"]
        content = result.get("content", "")

        print(f"  Agent state: {state}")
        print(f"  Agent content: {content[:100]}")

        assert state == "DONE", f"Expected DONE, got {state}"
        assert "mock" in content.lower(), f"Expected 'mock' in content, got {content}"
        print("  ✅ Agent (Mock) OK")
        return True

    except Exception as e:
        import traceback
        print(f"  ❌ Agent Failed: {e}")
        traceback.print_exc()
        return False
    finally:
        # 清理测试任务（先删 step 再删 task）
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)


async def test_agent_real_llm() -> bool:
    """测试 4: 真实 LLM 调用 (MiniMax 或 OpenAI)"""
    print("\n[4/4] Agent with Real LLM...")

    from src.utils.config import get_config
    cfg = get_config()
    llm_cfg = cfg.get("llm", {})
    providers = llm_cfg.get("providers", {})

    # 尝试找一个有 key 的 provider
    available = []
    for name, pcfg in providers.items():
        key = pcfg.get("api_key", "")
        if key and not key.startswith("${"):
            available.append((name, pcfg))

    if not available:
        print("  ⏭️  No LLM credentials available, skipping")
        return True

    pool = get_pool()
    task_id = uuid.uuid4()
    description = "What is 2+2? Answer in one sentence."

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO tasks (id, user_id, description, status, current_state, max_tool_calls, tool_call_count)
            VALUES ($1, $2, $3, 'running', 'IDLE', 50, 0)
            """,
            task_id, "e2e-test", description,
        )
        step_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO task_steps (id, task_id, seq, state, tool_name, input_args, attempt, max_attempts, ttl_seconds)
            VALUES ($1, $2, 0, 'pending', NULL, '{}', 1, 3, 300)
            """,
            step_id, task_id,
        )
        task_id_str = str(task_id)

    try:
        agent = WispAgent(task_id_str, profile="coding")
        result = await agent.run()
        state = result["state"]
        content = result.get("content", "")

        print(f"  Agent state: {state}")
        print(f"  Agent content: {content[:200]}")

        assert state == "DONE", f"Expected DONE, got {state}"
        assert len(content) > 0, "Expected non-empty content"
        print("  ✅ Agent (Real LLM) OK")
        return True

    except Exception as e:
        import traceback
        print(f"  ❌ Agent Real LLM Failed: {e}")
        traceback.print_exc()
        return False
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM task_steps WHERE task_id = $1", task_id)
            await conn.execute("DELETE FROM tasks WHERE id = $1", task_id)


# ── Main ────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("Wisp Agent End-to-End Verification")
    print("=" * 60)

    # 初始化 DB pool
    print("\n[boot] Initializing DB pool...")
    await init_pool()
    print("  DB pool initialized")

    results = []

    r = await test_db_connection()
    results.append(("DB Connection", r))

    r = await test_redis_connection()
    results.append(("Redis Connection", r))

    r = await test_agent_mock()
    results.append(("Agent (Mock)", r))

    r = await test_agent_real_llm()
    results.append(("Agent (Real LLM)", r))

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    all_pass = True
    for name, ok in results:
        status = "✅" if ok else "❌"
        print(f"  {status} {name}")
        if not ok:
            all_pass = False

    print()
    if all_pass:
        print("🎉 All checks passed!")
    else:
        print("⚠️  Some checks failed")

    # 关闭 DB pool
    await close_pool()

    return 0 if all_pass else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
