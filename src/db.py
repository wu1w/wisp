"""
PostgreSQL 异步连接池。

使用 asyncpg实现，符合 SPEC.md 第二章 2.2 节规范：
- 全局单例，禁止每次请求新建连接
- 所有表定义来自 src.models.tables
"""

from __future__ import annotations

# ── Pool Manager ───────────────────────────────────────────────
# 存储在 sys.modules 外部的全局注册表，避免 sys.modules['src.db'] 清除后丢失状态
import sys as _sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.utils.config import get_config

_POOL_REGISTRY_KEY = "_wisp_db_pool"


def _get_pool_from_registry() -> asyncpg.Pool | None:
    return _sys.modules.get(_POOL_REGISTRY_KEY)


def _set_pool_in_registry(pool: asyncpg.Pool) -> None:
    _sys.modules[_POOL_REGISTRY_KEY] = pool


def _clear_pool_in_registry() -> None:
    _sys.modules.pop(_POOL_REGISTRY_KEY, None)


# ── 公共 API ────────────────────────────────────────────────────

async def init_pool() -> None:
    """初始化全局 asyncpg 连接池（在应用启动时调用）。"""
    config = get_config()
    db_cfg = config["database"]

    pool = await asyncpg.create_pool(
        host=db_cfg["host"],
        port=db_cfg["port"],
        database=db_cfg["name"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        min_size=2,
        max_size=db_cfg.get("pool_size", 20),
    )
    # 验证连接
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1") == 1
    _set_pool_in_registry(pool)


async def close_pool() -> None:
    """关闭连接池（在应用关闭时调用）。"""
    pool = _get_pool_from_registry()
    if pool is not None:
        await pool.close()
        _clear_pool_in_registry()


def get_pool() -> asyncpg.Pool:
    """获取全局连接池。"""
    pool = _get_pool_from_registry()
    if pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return pool


@asynccontextmanager
async def acquire() -> AsyncIterator[asyncpg.Connection]:
    """
    从池中获取一个连接，用法：

        async with acquire() as conn:
            rows = await conn.fetch("SELECT * FROM tasks")
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


# ── SQLAlchemy 异步 Session（用于 ORM 场景） ────────────────────

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine() -> None:
    """初始化 SQLAlchemy 异步引擎（在应用启动时调用）。"""
    global _engine, _session_factory
    config = get_config()
    db_url = config.get("database_url") or _build_db_url(config["database"])

    _engine = create_async_engine(
        db_url,
        pool_size=config["database"].get("pool_size", 20),
        max_overflow=config["database"].get("max_overflow", 10),
        echo=False,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def close_engine() -> None:
    """关闭 SQLAlchemy 引擎。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    获取 SQLAlchemy 异步 Session，用法：

        async with get_session() as session:
            result = await session.execute(select(Task))
    """
    if _session_factory is None:
        raise RuntimeError("DB engine not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        yield session


def _build_db_url(db_cfg: dict[str, Any]) -> str:
    """从数据库配置字典构造 asyncpg URL。"""
    user = db_cfg["user"]
    password = db_cfg["password"]
    host = db_cfg["host"]
    port = db_cfg["port"]
    db_name = db_cfg["name"]
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db_name}"
