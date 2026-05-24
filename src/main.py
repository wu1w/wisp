"""Wisp FastAPI Application Entry Point。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api import approvals, dreaming, evolution, files, tasks, webui  # noqa: F401
from src.core.skills import load_skills
from src.db import close_engine, close_pool, init_engine, init_pool
from src.services import minio_client
from src.utils.health import validate_llm_credentials
from src.utils.rate_limit import limiter
from src.utils.tracing import setup_tracing

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.getLogger().level))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化连接池，关闭时释放资源。"""
    # 启动阶段
    await init_pool()      # asyncpg 连接池
    await init_engine()    # SQLAlchemy 异步引擎
    minio_client.init_minio()  # MinIO 客户端

    # Skill 加载：扫描 skills/ 目录，注册所有合法 Skill 为 Virtual Tool
    registered_skills = load_skills()
    skill_names = list(registered_skills.keys())

    logger = structlog.get_logger()

    # LLM 凭证校验（非阻塞，仅记录）
    llm_status = await validate_llm_credentials()
    for provider, ok in llm_status.items():
        if not ok:
            logger.warning("wisp_llm_provider_invalid", provider=provider)

    logger.info("wisp_startup_complete", llm_providers=llm_status, skills=skill_names)

    # OpenTelemetry 追踪初始化（自动注入 FastAPI Instrumentation）
    async with setup_tracing(app):
        yield

    # 关闭阶段
    await close_pool()
    await close_engine()
    logger.info("wisp_shutdown_complete")


app = FastAPI(
    title="Wisp API",
    version="0.1.0",
    lifespan=lifespan,
)

# Prometheus Metrics
instrumentator = Instrumentator()
instrumentator.instrument(app)
instrumentator.expose(app, endpoint="/metrics")

# Rate limit 异常处理
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(tasks.router, prefix="/v1/tasks", tags=["tasks"])
app.include_router(files.router, prefix="/v1/files", tags=["files"])
app.include_router(approvals.router, prefix="/v1/approvals", tags=["approvals"])
app.include_router(evolution.router, prefix="/v1/evolution", tags=["evolution"])
app.include_router(dreaming.router, prefix="/v1/dreaming", tags=["dreaming"])
app.include_router(webui.router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """健康检查端点。"""
    return {"status": "ok", "service": "wisp"}


@app.get("/healthz")
async def health_check_full() -> dict[str, dict[str, str]]:
    """
    完整健康检查：DB、Redis、MinIO、LLM、Worker
    """
    from src.db import get_pool
    from src.utils.config import get_config

    config = get_config()
    components: dict[str, dict[str, str]] = {}

    # Database
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        components["postgresql"] = {"status": "ok", "detail": config["database"]["host"]}
    except Exception as exc:
        components["postgresql"] = {"status": "error", "detail": str(exc)[:100]}

    # Redis
    try:
        import redis.asyncio as redis
        r = redis.from_url(
            f"redis://{config['redis']['host']}:{config['redis']['port']}",
            decode_responses=True,
        )
        await r.ping()  # type: ignore[misc]
        await r.aclose()
        components["redis"] = {"status": "ok", "detail": config["redis"]["host"]}
    except Exception as exc:
        components["redis"] = {"status": "error", "detail": str(exc)[:100]}

    # MinIO
    try:
        mc = minio_client._get_client()
        buckets = mc.list_buckets()
        components["minio"] = {"status": "ok", "detail": f"{len(buckets)} bucket(s)"}
    except Exception as exc:
        components["minio"] = {"status": "error", "detail": str(exc)[:100]}

    # LLM providers
    try:
        llm_status = await validate_llm_credentials()
        for provider, ok in llm_status.items():
            components[f"llm_{provider}"] = {
                "status": "ok" if ok else "invalid_key",
                "detail": provider,
            }
    except Exception as exc:
        components["llm"] = {"status": "error", "detail": str(exc)[:100]}

    return components

