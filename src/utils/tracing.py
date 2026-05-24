"""OpenTelemetry 追踪：OTLP 导出、Async Span、LLM 专用语义约定。"""

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import structlog

from src.utils.config import get_config

logger = structlog.get_logger(__name__)

# ── OTEL 初始化（惰性单例）─────────────────────────────────────

_tracer: "Any" = None  # opentelemetry.sdk.trace.Tracer，避免运行时 import
_initialized: bool = False


def _init_tracing() -> None:
    """
    初始化 OpenTelemetry SDK + OTLP 导出器。

    仅执行一次。环境变量 OTEL_SDK_DISABLE 默认 false，
    OTEL_EXPORTER_OTLP_ENDPOINT 未配置时自动降级为 no-op。
    """
    global _tracer, _initialized
    if _initialized:
        return
    _initialized = True

    if os.getenv("OTEL_SDK_DISABLE", "").lower() in ("1", "true", "yes"):
        logger.info("otel_disabled_by_env")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLGPyGRPCSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        logger.warning("opentelemetry_deps_not_installed")
        return

    cfg = get_config().get("tracing", {})
    if not cfg.get("enabled", False):
        logger.info("otel_disabled_in_config")
        return

    service_name = cfg.get("service_name", "wisp")
    otlp_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        cfg.get("otlp_endpoint", ""),
    )

    resource = Resource.create({"service.name": service_name})

    # 采样策略：OTEL_TRACES_SAMPLER 环境变量支持
    sampler = _build_sampler(os.getenv("OTEL_TRACES_SAMPLER", "always_on"))

    provider = TracerProvider(resource=resource, sampler=sampler)

    if otlp_endpoint:
        try:
            exporter = OTLGPyGRPCSpanExporter(endpoint=otlp_endpoint, insecure=True)
            # 生产用 BatchSpanProcessor，测试用 SimpleSpanProcessor
            processor: Any = (
                BatchSpanProcessor(exporter)
                if os.getenv("OTEL_BATCH", "true").lower() != "false"
                else SimpleSpanProcessor(exporter)
            )
            provider.add_span_processor(processor)
            logger.info("otel_init_ok", endpoint=otlp_endpoint, service=service_name)
        except Exception as exc:
            logger.warning("otel_exporter_init_failed", error=str(exc))
    else:
        logger.info("otel_no_endpoint_no_export")

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)


def _build_sampler(sampler_arg: str):
    """根据环境变量构建采样器。"""
    from opentelemetry.sdk.trace.sampling import (
        AlwaysOffSampler,
        AlwaysOnSampler,
        ParentBased,
        TraceIdRatioBased,
    )

    mapping = {
        "always_on": AlwaysOnSampler(),
        "always_off": AlwaysOffSampler(),
        "always_on|parent": ParentBased(AlwaysOnSampler()),
        "always_off|parent": ParentBased(AlwaysOffSampler()),
    }
    if sampler_arg.startswith("trace_id_ratio="):
        ratio = float(sampler_arg.split("=", 1)[1])
        return ParentBased(TraceIdRatioBased(ratio))
    return mapping.get(sampler_arg, AlwaysOnSampler())


def get_tracer() -> "Any":
    """获取（或初始化）Tracer 实例。"""
    if not _initialized:
        _init_tracing()
    return _tracer


# ── 类型别名 ────────────────────────────────────────────────────

F = TypeVar("F", bound=Callable[..., Any])


# ── 追踪装饰器 ─────────────────────────────────────────────────

def traced(
    name: str | None = None,
    span_type: str = "internal",
    attributes: dict[str, Any] | None = None,
) -> "Any":
    """
    Async Span 装饰器。

    用法：
        @traced("llm.chat", span_type="llm", attributes={"provider": "openai"})
        async def chat(...):
            ...

        @traced("tool.call", span_type="tool")
        async def call_tool(...):
            ...

    特性：
    - 自动捕获函数返回值作为 span 属性
    - 自动 Record 异常
    - 支持嵌套 span（子 span 自动继承父 trace context）
    - LLM 类型 span 记录 input_tokens / output_tokens / model
    """
    _init_tracing()

    def decorator(func: F) -> F:
        op_name = name or func.__name__
        extra_attrs = attributes or {}

        # 尝试识别 LLM 调用，自动注入标准语义属性
        is_llm = span_type == "llm"

        import functools

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            if tracer is None:
                # OTEL 不可用，降级为直接调用
                return await func(*args, **kwargs)

            span_name = op_name
            span_kind: Any  # opentelemetry.trace.SpanKind
            try:
                from opentelemetry import trace
                span_kind = (
                    trace.SpanKind.CLIENT if is_llm else trace.SpanKind.INTERNAL
                )
            except ImportError:
                span_kind = None

            extra_attrs_copy = dict(extra_attrs)

            try:
                with tracer.start_as_current_span(
                    span_name,
                    kind=span_kind,
                    attributes={
                        "span_type": span_type,
                        "function": func.__qualname__,
                        **extra_attrs_copy,
                    },
                ) as span:
                    try:
                        result = await func(*args, **kwargs)

                        # LLM span：尝试从返回值提取 usage
                        if is_llm and isinstance(result, dict):
                            usage = result.get("usage", {})
                            if usage:
                                span.set_attribute("llm.usage.prompt_tokens", usage.get("prompt_tokens", 0))
                                span.set_attribute("llm.usage.completion_tokens", usage.get("completion_tokens", 0))
                                span.set_attribute("llm.usage.total_tokens", usage.get("total_tokens", 0))
                            model = result.get("model")
                            if model:
                                span.set_attribute("llm.model", model)
                            provider = result.get("provider")
                            if provider:
                                span.set_attribute("llm.provider", provider)

                        span.set_attribute("error", False)
                        return result

                    except Exception as exc:
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(exc))
                        span.set_attribute("error.type", type(exc).__name__)
                        span.record_exception(exc, exc_info=True)
                        raise

            except Exception:
                # OTEL span 创建失败，降级直接调用
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


# ── 同步版本（用于普通函数） ────────────────────────────────────

def traced_sync(
    name: str | None = None,
    span_type: str = "internal",
    attributes: dict[str, Any] | None = None,
) -> "Any":
    """
    同步 Span 装饰器（用于非 async 函数）。
    """
    _init_tracing()

    def decorator(func: F) -> F:
        op_name = name or func.__name__
        extra_attrs = dict(attributes or {})

        import functools

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = get_tracer()
            if tracer is None:
                return func(*args, **kwargs)

            try:
                from opentelemetry import trace
                kind = trace.SpanKind.INTERNAL
            except ImportError:
                kind = None

            try:
                with tracer.start_as_current_span(op_name, kind=kind, attributes={
                    "span_type": span_type,
                    "function": func.__qualname__,
                    **extra_attrs,
                }) as span:
                    try:
                        result = func(*args, **kwargs)
                        span.set_attribute("error", False)
                        return result
                    except Exception as exc:
                        span.set_attribute("error", True)
                        span.set_attribute("error.message", str(exc))
                        span.set_attribute("error.type", type(exc).__name__)
                        span.record_exception(exc, exc_info=True)
                        raise
            except Exception:
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


# ── 上下文管理器（用于手动 span 范围） ──────────────────────────

@asynccontextmanager
async def span(
    name: str,
    span_type: str = "internal",
    attributes: dict[str, Any] | None = None,
):
    """
    手动 Span 上下文管理器。

    用法：
        async with span("my_operation", span_type="db") as span_obj:
            span_obj.set_attribute("db.system", "postgresql")
            await do_work()
    """
    _init_tracing()
    tracer = get_tracer()

    attrs = {"span_type": span_type, **(attributes or {})}

    if tracer is None:
        yield None
        return

    try:
        from opentelemetry import trace
        kind = trace.SpanKind.INTERNAL
    except ImportError:
        kind = None

    try:
        with tracer.start_as_current_span(name, kind=kind, attributes=attrs) as span_obj:
            yield span_obj
    except Exception:
        yield None


# ── FastAPI / uvicorn 生命周期集成 ─────────────────────────────

@asynccontextmanager
async def setup_tracing(app: "Any" = None) -> "Any":
    """
    FastAPI 应用启动时调用，初始化 OTEL 并注册 FastAPI Instrumentation。

    用法（main.py lifespan）：
        async with setup_tracing(app):
            yield
    """
    _init_tracing()

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("fastapi_otel_instrumentation_not_installed")
        yield
        return

    if app is not None:
        try:
            FastAPIInstrumentor.instrument_app(app)
            logger.info("fastapi_otel_instrumented")
        except Exception as exc:
            logger.warning("fastapi_otel_instrumentation_failed", error=str(exc))

    try:
        yield
    finally:
        try:
            from opentelemetry import trace
            provider = trace.get_tracer_provider()
            if hasattr(provider, "shutdown"):
                provider.shutdown()
        except Exception:
            pass
