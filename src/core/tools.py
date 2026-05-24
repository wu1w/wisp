"""Agent 工具定义与注册中心。"""

import asyncio
import json
import os
from collections.abc import Callable
from typing import Any

import structlog

from src.services.sandbox import SandboxUnavailableError, sandbox_service
from src.utils.security import SecurityError, validate_path

logger = structlog.get_logger(__name__)


class ToolRegistry:
    """工具注册表：所有 Agent 可调用的工具在此注册。"""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        """注册一个工具。"""
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        self._handlers[name] = handler

    def get_tool(self, name: str) -> dict[str, Any] | None:
        """获取工具定义（用于 Function Calling Schema）。"""
        return self._tools.get(name)

    def get_all_tools(self) -> list[dict[str, Any]]:
        """获取所有工具定义。"""
        return list(self._tools.values())

    async def call(self, name: str, **kwargs: Any) -> Any:
        """调用工具处理器。"""
        if name not in self._handlers:
            raise ValueError(f"Unknown tool: {name}")
        handler = self._handlers[name]
        return await handler(**kwargs)


# ── 工具实现 ──────────────────────────────────────────────────

_MAX_BASH_OUTPUT = 10_000  # 输出截断阈值


async def _bash_impl(command: str, timeout: int = 60, workdir: str | None = None) -> dict[str, Any]:
    """
    在沙箱执行 bash 命令，沙箱不可用时降级为本地受限执行。

    优先级：
    1. 尝试 Docker 沙箱（隔离网络 + 资源限制）
    2. 沙箱不可用时，降级本地执行（仍应用危险命令黑名单）

    安全限制（本地降级时）：
    - 禁止交互式命令
    - 超时强制杀死
    - 输出截断到 10000 字符
    """
    # 危险命令黑名单（即使在沙箱内也做基础过滤）
    _dangerous = frozenset([
        "rm -rf /", "mkfs", "dd if=", ">/dev/sd", "chmod 777 /",
        "shutdown", "reboot", "init 0", "init 6",
        "curl -s http", "wget http",
    ])
    cmd_lower = command.lower()
    for pat in _dangerous:
        if pat in cmd_lower:
            return {
                "stdout": "",
                "stderr": f"Command blocked by security policy: {pat}",
                "exit_code": 126,
            }

    # 优先尝试沙箱执行
    if sandbox_service.is_available():
        try:
            result = await sandbox_service.execute(
                command=command,
                lang="bash",
                timeout=timeout,
                workdir=workdir or "/workspace",
            )
            logger.debug("bash_via_sandbox", exit_code=result["exit_code"])
            return result
        except SandboxUnavailableError:
            logger.warning("sandbox_unavailable_falling_back_to_local")

    # 沙箱不可用，降级本地执行
    logger.warning("bash_local_execution_warning")
    cwd = workdir or os.getcwd()
    try:
        proc = await _run_process(
            ["bash", "-lc", command],
            cwd=cwd,
            timeout=timeout,
        )
    except TimeoutError:
        return {
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "exit_code": 124,
        }

    stdout = proc["stdout"][: _MAX_BASH_OUTPUT]
    stderr = proc["stderr"][: 2000]

    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": proc["exit_code"],
        "timed_out": proc.get("timed_out", False),
    }


async def _run_process(args: list[str], cwd: str, timeout: int) -> dict[str, Any]:
    """在子进程执行命令，带超时。"""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        timed_out = False
    except TimeoutError:
        proc.kill()
        try:
            stdout, stderr = await proc.communicate()
        except Exception:
            stdout = b""
            stderr = b""
        timed_out = True

    return {
        "stdout": stdout.decode("utf-8", errors="replace") if stdout else "",
        "stderr": stderr.decode("utf-8", errors="replace") if stderr else "",
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
    }


async def _read_file_impl(path: str, max_chars: int = 5000) -> dict[str, Any]:
    """读取文件内容（白名单路径校验）。"""
    try:
        p = validate_path(path, allow_create=False)
    except SecurityError as exc:
        return {"error": f"SecurityError: {exc}", "content": None, "truncated": False}

    if not p.is_file():
        return {"error": f"Not a file: {path}", "content": None, "truncated": False}

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": f"Read error: {exc}", "content": None, "truncated": False}

    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + f"\n... (truncated, total {len(content)} chars)"

    return {"content": content, "truncated": truncated, "error": None}


async def _write_file_impl(
    path: str,
    content: str,
    append: bool = False,
    task_id: str | None = None,
) -> dict[str, Any]:
    """
    写入文件（白名单路径校验，自动创建父目录）。

    版本管理：若文件已存在，自动创建版本快照（Copy-on-Write）。
    """
    try:
        p = validate_path(path, allow_create=True)
    except SecurityError as exc:
        return {"error": f"SecurityError: {exc}", "path": None}

    try:
        p.parent.mkdir(parents=True, exist_ok=True)

        # 版本快照（仅非 append 模式且文件已存在时）
        version_info = None
        if not append and p.exists() and p.stat().st_size > 0:
            try:
                from src.services.file_versioning import write_file_version
                version_info = await write_file_version(
                    original_path=str(p),
                    content=p.read_text(encoding="utf-8"),  # 快照当前内容
                    task_id=task_id,
                )
                logger.debug("file_version_created", path=str(p), version=version_info["version_tag"])
            except Exception as exc:
                # 版本创建失败不影响写入，只记录 warning
                logger.warning("write_file_version_failed", path=str(p), error=str(exc))

        # 执行写入
        with open(p, "a" if append else "w", encoding="utf-8") as fh:
            fh.write(content)

    except Exception as exc:
        return {"error": f"Write error: {exc}", "path": None}

    return {
        "path": str(p),
        "bytes": len(content),
        "version": version_info,
        "error": None,
    }


async def _list_dir_impl(path: str = ".") -> dict[str, Any]:
    """列出目录内容。"""
    try:
        p = validate_path(path, allow_create=False)
    except SecurityError as exc:
        return {"error": f"SecurityError: {exc}", "entries": None}

    if not p.is_dir():
        return {"error": f"Not a directory: {path}", "entries": None}

    try:
        entries = []
        for entry in p.iterdir():
            stat = entry.stat()
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": stat.st_size,
            })
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"]))
    except PermissionError:
        return {"error": f"Permission denied: {path}", "entries": None}
    except Exception as exc:
        return {"error": f"Read error: {exc}", "entries": None}

    return {"entries": entries, "path": str(p), "error": None}


async def _search_memory_impl(query: str, top_k: int = 5) -> dict[str, Any]:
    """向量相似度搜索记忆。"""
    try:
        from src.services.memory import memory_service
        results = await memory_service.search(query=query, top_k=top_k)
        return {
            "results": [
                {"content": r["content"][:500], "score": r["score"], "memory_type": r.get("type")}
                for r in results
            ],
            "error": None,
        }
    except Exception as exc:
        logger.warning("search_memory_failed", error=str(exc))
        return {"results": [], "error": str(exc)}


# ── reflect_on_error ─────────────────────────────────────────

_REFLECT_PROMPT_TEMPLATE = """\
You are Wisp's error reflection engine. Analyze the failed command execution and provide actionable insights.

Failed Command: {failed_command}
Exit Code: {exit_code}
Error Message: {error_message}
Attempt: {attempt}

Based on the error message and exit code, provide:
1. root_cause: What went wrong? Be specific (e.g. "pip install failed because network is restricted in sandbox")
2. fix_suggestion: Concrete steps to fix the issue
3. new_command: A corrected version of the failed command, if retryable (otherwise null)
4. should_retry: Whether the agent should retry with the new_command (true if there's a plausible fix)
5. error_type: Categorize the error (PermissionError / SyntaxError / NotFoundError / NetworkError / ResourceError / Unknown)

Respond in JSON format with keys: root_cause, fix_suggestion, new_command, should_retry, error_type
"""


async def _reflect_on_error_impl(
    task_id: str,
    error_message: str,
    exit_code: int,
    failed_command: str,
    attempt: int = 1,
) -> dict[str, Any]:
    """
    错误反思工具（系统强制触发，禁止调用 bash 等执行类工具）。

    触发条件：bash 工具返回 exit_code != 0

    返回：
        analysis: 根因分析
        fix_suggestion: 修复建议
        new_command: 修正后的命令（不可重试时为 null）
        should_retry: 是否应该重试
        error_type: 错误分类
    """
    root_cause = ""
    fix_suggestion = ""
    new_command_val: str | None = None
    should_retry = False
    error_type = "Unknown"
    reflection_error: str | None = None

    try:
        import re

        from src.core.llm.gateway import LLMGateway
        from src.models.schemas import LLMMessage

        gateway = LLMGateway()
        messages = [
            LLMMessage(
                role="user",
                content=_REFLECT_PROMPT_TEMPLATE.format(
                    failed_command=failed_command,
                    exit_code=exit_code,
                    error_message=error_message,
                    attempt=attempt,
                ),
            )
        ]

        response = await gateway.chat(
            messages=messages,
            profile="cheap",  # 反思不需要强推理模型
            tools=None,
            temperature=0.1,
            max_tokens=600,
        )

        content = response.content or "{}"
        # 提取 JSON（可能包含 markdown 代码块）
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
        else:
            parsed = json.loads(content)

        root_cause = parsed.get("root_cause", "Unknown error")
        fix_suggestion = parsed.get("fix_suggestion", "")
        new_command_val = parsed.get("new_command")
        should_retry = bool(parsed.get("should_retry", False))
        error_type = parsed.get("error_type", "Unknown")

    except Exception as exc:
        logger.warning("reflect_on_error_llm_call_failed", error=str(exc))
        reflection_error = str(exc)
        root_cause = f"Reflection failed: {exc}"
        fix_suggestion = ""
        new_command_val = None
        should_retry = False
        error_type = "Unknown"

    # 写入 reflection_reports 表（按设计文档规范）
    try:
        import uuid as uuid_mod

        from src.db import acquire

        async with acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reflection_reports
                    (id, task_id, error_type, root_cause, fix_suggestion, prompt_delta)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                uuid_mod.uuid4(),
                uuid_mod.UUID(task_id),
                error_type,
                root_cause,
                fix_suggestion,
                new_command_val,  # prompt_delta 复用 new_command 字段
            )
    except Exception as db_exc:
        logger.warning("reflection_report_save_failed", error=str(db_exc))

    return {
        "analysis": root_cause,
        "fix_suggestion": fix_suggestion,
        "new_command": new_command_val,
        "should_retry": should_retry,
        "error_type": error_type,
        "error": reflection_error,
    }


# ── save_memory ──────────────────────────────────────────────

async def _save_memory_impl(
    type: str,  # episodic | procedural | semantic | reflective
    content: str,
    task_id: str,
    success: bool,
    tool_name: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    保存记忆到记忆库（ETL Pipeline 自动触发，Agent 不可见结果）。

    参数：
        type: 记忆类型（episodic/procedural/semantic/reflective）
        content: 记忆内容
        task_id: 关联任务 ID
        success: 命令是否成功执行
        tool_name: 关联工具名
        metadata: 额外元数据
    """
    valid_types = ("episodic", "procedural", "semantic", "reflective")
    if type not in valid_types:
        return {"error": f"Invalid memory type: {type!r}. Must be one of {valid_types}"}

    try:
        from src.services.memory import MemoryType, memory_service

        memory_id = await memory_service.save(
            type=MemoryType(type),
            content=content,
            task_id=task_id,
            tool_name=tool_name,
            success=success,
            metadata=metadata or {},
        )
        return {"memory_id": memory_id, "error": None}
    except Exception as exc:
        logger.warning("save_memory_failed", error=str(exc), type=type, task_id=task_id)
        return {"error": str(exc)}


# ── 注册内置工具 ──────────────────────────────────────────────

registry = ToolRegistry()

registry.register(
    name="bash",
    description=(
        "Execute a bash command in an isolated environment. "
        "Use for file operations, git, grep, curl (via proxy), etc. "
        "Do NOT use for interactive programs (vim/less/nano). "
        "Network access is restricted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 60, max 300).",
                "default": 60,
            },
            "workdir": {
                "type": "string",
                "description": "Working directory for the command.",
            },
        },
        "required": ["command"],
    },
    handler=_bash_impl,
)

registry.register(
    name="read_file",
    description="Read the content of a file. Returns up to 5000 characters with truncation flag.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to read."},
            "max_chars": {
                "type": "integer",
                "description": "Maximum characters to read (default 5000).",
                "default": 5000,
            },
        },
        "required": ["path"],
    },
    handler=_read_file_impl,
)

registry.register(
    name="write_file",
    description="Write content to a file. Creates parent directories if needed. Overwrites by default.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to write."},
            "content": {"type": "string", "description": "Content to write."},
            "append": {
                "type": "boolean",
                "description": "Append to file instead of overwriting.",
                "default": False,
            },
        },
        "required": ["path", "content"],
    },
    handler=_write_file_impl,
)

registry.register(
    name="list_dir",
    description="List directory contents with file sizes and types.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path to list (default current directory).",
                "default": ".",
            },
        },
    },
    handler=_list_dir_impl,
)

registry.register(
    name="search_memory",
    description="Search the agent's episodic/procedural memory using vector similarity.",
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language search query."},
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    handler=_search_memory_impl,
)

registry.register(
    name="reflect_on_error",
    description=(
        "Reflect on a failed tool execution. Automatically triggered when bash returns exit_code != 0. "
        "DO NOT call bash or other execution tools from within this tool. "
        "Returns root cause analysis, fix suggestion, and whether to retry."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task ID this error belongs to."},
            "error_message": {"type": "string", "description": "The error message from the failed execution."},
            "exit_code": {"type": "integer", "description": "The exit code of the failed command."},
            "failed_command": {"type": "string", "description": "The command that failed."},
            "attempt": {"type": "integer", "description": "Attempt number (default 1).", "default": 1},
        },
        "required": ["task_id", "error_message", "exit_code", "failed_command"],
    },
    handler=_reflect_on_error_impl,
)

registry.register(
    name="save_memory",
    description=(
        "Save a memory to the agent's memory bank. Triggered automatically by the ETL pipeline. "
        "This tool is for internal system use; the agent does not see its result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "description": "Memory type: episodic | procedural | semantic | reflective",
                "enum": ["episodic", "procedural", "semantic", "reflective"],
            },
            "content": {"type": "string", "description": "Memory content (max 5000 chars)."},
            "task_id": {"type": "string", "description": "Associated task ID."},
            "success": {"type": "boolean", "description": "Whether the associated tool call succeeded."},
            "tool_name": {"type": "string", "description": "Name of the associated tool."},
            "metadata": {"type": "object", "description": "Additional metadata."},
        },
        "required": ["type", "content", "task_id", "success"],
    },
    handler=_save_memory_impl,
)
