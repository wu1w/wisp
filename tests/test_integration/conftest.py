"""
Integration test fixtures：PostgreSQL、Redis、MinIO。

依赖服务必须先启动：
    docker-compose -f docker/docker-compose.yml up -d postgres redis minio init-minio
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import asyncpg
import pytest

# ── 环境 ────────────────────────────────────────────────────

def _database_url_from_env() -> str:
    user = os.getenv("DATABASE_USER", "wisp")
    password = os.getenv("DATABASE_PASSWORD", "wisp")
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    db = os.getenv("DATABASE_DB", "wisp")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _redis_url_from_env() -> str:
    password = os.getenv("REDIS_PASSWORD", "")
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "6379")
    if password:
        return f"redis://:{password}@{host}:{port}/0"
    return f"redis://{host}:{port}/0"


# ── DB ─────────────────────────────────────────────────────

@pytest.fixture
def database_url() -> str:
    return os.getenv("DATABASE_URL") or _database_url_from_env()


@pytest.fixture
async def db_pool(database_url: str) -> Any:
    """
    为每个测试创建独立的 asyncpg 连接池。

    使用 sys.modules 外部注册表存储池，以避免 sys.modules['src.db'] 清除后丢失状态。
    """
    import src.db as db_module

    pool = await asyncpg.create_pool(
        database_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )
    db_module._set_pool_in_registry(pool)
    yield pool
    db_module._clear_pool_in_registry()
    await pool.close()


@pytest.fixture
async def db_conn(db_pool: Any) -> Any:
    """提供单个数据库连接的 fixture（依赖 db_pool）。"""
    async with db_pool.acquire() as conn:
        yield conn


# ── Redis ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def redis_url() -> str:
    return os.getenv("REDIS_URL") or _redis_url_from_env()


@pytest.fixture(scope="session")
async def redis_client(redis_url: str) -> Any:
    import redis.asyncio as redis

    client = redis.from_url(redis_url, decode_responses=True)
    await client.ping()
    yield client
    await client.aclose()


# ── MinIO ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def minio_config() -> dict[str, str]:
    return {
        "endpoint": os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        "access_key": os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        "secret_key": os.getenv("MINIO_SECRET_KEY", "minioadmin"),
        "bucket": "wisp",
        "secure": False,
    }


@pytest.fixture(scope="session")
async def minio_client(minio_config: dict[str, str]) -> Any:
    from minio import Minio

    client = Minio(
        endpoint=minio_config["endpoint"],
        access_key=minio_config["access_key"],
        secret_key=minio_config["secret_key"],
        secure=minio_config["secure"],
    )
    if not client.bucket_exists(minio_config["bucket"]):
        client.make_bucket(minio_config["bucket"])
    yield client


# ── Helpers ─────────────────────────────────────────────────

@pytest.fixture
def task_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def user_id() -> str:
    return "test-user-001"
