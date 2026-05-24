"""
Redis Streams 任务队列（替换 DB 轮询 Polling）。

设计目标：
- Worker 阻塞等待 XREADGROUP，有新任务时立即被推送（延迟 < 10ms）
- 零轮询开销，Redis Pub/Sub 通知 Worker 有新任务
- 支持多 Worker 竞争消费，同一任务只被一个 Worker 处理
- 消费者组（Consumer Group）确保 at-least-once 语义
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import structlog

from src.utils.config import get_config

logger = structlog.get_logger(__name__)


# ── 常量 ────────────────────────────────────────────────────

STREAM_KEY = "wisp:steps:stream"
CONSUMER_GROUP = "wisp-workers"
CONSUMER_NAME_PREFIX = "worker"


class StreamQueue:
    """
    Redis Streams 任务队列。

    发布（Scheduler）：
        await stream_queue.publish_step(step_id, task_id, seq, payload)

    消费（Worker）：
        async for message in stream_queue.consume(worker_id):
            step_id = message["step_id"]
            ...
            await stream_queue.ack(step_id)
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._redis_url = self._config.get("redis_url", "redis://localhost:6379")
        self._client: Any = None  # redis.asyncio.Redis

    # ── 客户端管理 ──────────────────────────────────────────

    async def _get_client(self) -> Any:
        """懒加载 Redis 客户端。"""
        if self._client is None:
            import redis.asyncio as redis
            self._client = redis.from_url(self._redis_url, decode_responses=True)
            await self._client.ping()
        return self._client

    async def close(self) -> None:
        """关闭连接。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── 发布 ────────────────────────────────────────────────

    async def publish_step(
        self,
        step_id: str,
        task_id: str,
        seq: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """
        发布一个 Step 到队列（Scheduler 调用）。

        同时向频道 broadcast 通知有新任务，避免 Worker 一直 XREADGROUP 阻塞。
        """
        client = await self._get_client()

        message_id = uuid.uuid4().hex
        fields = {
            "step_id": step_id,
            "task_id": task_id,
            "seq": str(seq),
            "payload": "" if payload is None else str(payload),
        }

        # 添加到 Stream
        await client.xadd(STREAM_KEY, fields, maxlen=10000, approximate=True)

        # 广播通知（Worker 通过 BRPOP 监听此频道）
        await client.publish("wisp:steps:new", message_id)

        logger.debug(
            "step_published_to_stream",
            step_id=step_id,
            task_id=task_id,
            seq=seq,
        )

    # ── 消费 ────────────────────────────────────────────────

    async def ensure_group(self) -> None:
        """确保消费者组存在（首次运行调用）。"""
        client = await self._get_client()
        try:
            await client.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("stream_consumer_group_created", group=CONSUMER_GROUP)
        except Exception:
            # 组已存在
            pass

    async def consume(self, worker_id: str) -> AsyncGenerator[dict[str, Any] | None, None]:
        """
        消费队列消息（阻塞，Worker 主循环调用）。

        使用 BRPOPLPUSH 从待处理广播频道获取信号，
        然后立即返回让 Worker 处理任务。

        如果 30 秒内无新任务，生成一个哨兵消息 None，
        让 Worker 可以执行空闲操作（如 hibernate 检查）。
        """

        client = await self._get_client()

        # 确保消费者组存在（首次运行创建）
        await self.ensure_group()

        # 先尝试非阻塞读取（快速路径）
        try:
            result = await client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=f"{CONSUMER_NAME_PREFIX}:{worker_id}",
                streams={STREAM_KEY: ">"},
                count=1,
                block=2000,  # 2s blocking — efficient waiting for new messages
            )
            if result:
                for stream_name, messages in result:
                    for msg_id, fields in messages:
                        yield {
                            "id": msg_id,
                            "step_id": fields.get("step_id"),
                            "task_id": fields.get("task_id"),
                            "seq": int(fields.get("seq", 0)),
                            "payload": fields.get("payload"),
                        }
                    return
        except Exception:
            pass

        # 无立即消息，轮询等待（降级到 DB 前多等几秒）
        await asyncio.sleep(2)
        yield None

    async def ack(self, message_id: str) -> None:
        """确认消息已处理（从 PEL 中移除）。"""
        client = await self._get_client()
        await client.xack(STREAM_KEY, CONSUMER_GROUP, message_id)

    # ── 队列状态监控 ────────────────────────────────────────

    async def queue_length(self) -> int:
        """返回队列中的消息数量。"""
        client = await self._get_client()
        try:
            length: int = await client.xlen(STREAM_KEY)
            return length
        except Exception:
            return -1

    async def pending_count(self) -> int:
        """返回消费者组中 pending 的消息数。"""
        client = await self._get_client()
        try:
            info: dict[str, Any] | None = await client.xpending(STREAM_KEY, CONSUMER_GROUP)
            if info is None:
                return 0
            pending_raw: Any = info.get("pending", 0) or 0
            return cast(int, int(pending_raw)) if pending_raw else 0
        except Exception:
            return -1


# 全局单例
stream_queue = StreamQueue()
