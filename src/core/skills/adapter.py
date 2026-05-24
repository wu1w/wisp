"""
Skill Adapter — Skill 执行适配器。

当 LLM 调用某个已注册的 Skill Tool 时，由 Adapter 负责：
1. 从 manifest 读取资源限制（timeout、network）
2. 构造执行命令（python skills/<name>/main.py --params <json>）
3. 在沙箱或子进程中执行，受资源限制约束
4. 解析 JSON 输出，返回标准化结果

安全设计：
- 网络访问由 manifest.network 控制（允许时不禁用网络，禁止时通过环境变量告知 skill）
- 超时硬限制来自 manifest.timeout
- Skill 执行路径严格限制在 skills/<name>/ 下（白名单）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

from src.core.skills.manifest import SkillManifest
from src.services.sandbox import SandboxUnavailableError, sandbox_service

logger = structlog.get_logger(__name__)


class SkillExecutionError(Exception):
    """Skill 执行失败。"""
    pass


class SkillAdapter:
    """
    Skill 执行适配器。

    将 Skill 执行为独立子进程，通过 stdin/stdout 传递参数和结果。
    资源限制由 manifest.limits 控制。
    """

    def __init__(self) -> None:
        self._manifests: dict[str, SkillManifest] = {}

    def register(self, manifest: SkillManifest) -> None:
        """注册一个 Skill（由 registry 调用）。"""
        self._manifests[manifest.tool_name] = manifest

    def create_handler(self, manifest: SkillManifest):
        """
        创建一个 async 工具处理器函数，注册到 ToolRegistry。

        返回值是一个可以被 `await registry.call(name, **kwargs)` 调用的协程。
        """

        async def skill_handler(**kwargs: Any) -> dict[str, Any]:
            return await self.execute(manifest.tool_name, kwargs)

        return skill_handler

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """
        执行指定的 Skill。

        参数：
            tool_name: Skill 的 tool_name（来自 manifest.tool_schema.name）
            arguments: LLM 传入的参数字典
        """
        manifest = self._manifests.get(tool_name)
        if manifest is None:
            raise SkillExecutionError(f"Skill '{tool_name}' not found in adapter")

        skill_path = manifest.skill_path
        if skill_path is None:
            raise SkillExecutionError(f"Skill '{tool_name}' has no skill_path")

        main_py = (skill_path / "main.py").resolve()

        if not main_py.exists():
            raise SkillExecutionError(
                f"Skill main.py not found: {main_py}"
            )

        # 白名单：禁止路径遍历
        try:
            resolved = main_py.resolve()
            if not str(resolved).startswith(str(skill_path.resolve())):
                raise SkillExecutionError("Path traversal detected")
        except Exception as exc:
            raise SkillExecutionError(f"Path validation failed: {exc}")

        logger.info(
            "skill_execute_start",
            tool_name=tool_name,
            skill=manifest.name,
            args_keys=list(arguments.keys()),
        )

        timeout = manifest.timeout_seconds

        # 如果 skill 需要网络，但沙箱可用，则尝试在沙箱执行
        if manifest.allow_network and sandbox_service.is_available():
            return await self._execute_in_sandbox(
                manifest=manifest,
                main_py=main_py,
                arguments=arguments,
                timeout=timeout,
            )

        # 本地执行（无沙箱或 skill 不需要网络）
        return await self._execute_local(
            manifest=manifest,
            main_py=main_py,
            arguments=arguments,
            timeout=timeout,
        )

    async def _execute_in_sandbox(
        self,
        manifest: SkillManifest,
        main_py: Path,
        arguments: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        """通过 Docker 沙箱执行 Skill。"""
        command = f"{sys.executable} {main_py} --params-json -"
        lang = "bash"

        try:
            result = await sandbox_service.execute(
                command=command,
                lang=lang,
                timeout=min(timeout, 300),
                workdir=str(main_py.parent),
            )
            return self._parse_skill_output(
                result,
                tool_name=manifest.tool_name,
            )
        except SandboxUnavailableError:
            logger.warning("sandbox_unavailable_falling_back_to_local")
            return await self._execute_local(
                manifest=manifest,
                main_py=main_py,
                arguments=arguments,
                timeout=timeout,
            )

    async def _execute_local(
        self,
        manifest: SkillManifest,
        main_py: Path,
        arguments: dict[str, Any],
        timeout: int,
    ) -> dict[str, Any]:
        """本地子进程执行 Skill（受 timeout 约束）。"""
        # 构造环境变量
        env: dict[str, str] = {
            "SKILL_NAME": manifest.name,
            "SKILL_VERSION": manifest.version,
            "SKILL_TOOL_NAME": manifest.tool_name,
            # 网络控制：skill 自己检查 SKILL_ALLOW_NETWORK=1 来决定是否联网
            "SKILL_ALLOW_NETWORK": "1" if manifest.allow_network else "0",
        }

        # 如果不允许网络，从环境继承（可能已有代理配置）
        # 注意：这里不能强制切断网络，因为是子进程，非 root 无法修改 namespace
        # 实际网络隔离由调用方（沙箱容器）保证

        params_json = json.dumps(arguments, ensure_ascii=False)

        # 使用临时文件传递参数（避免命令行转义问题）
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(params_json)
            params_file = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(main_py),
                "--params-json",
                params_file,
                env={**os.environ, **env},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(main_py.parent),
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                raise SkillExecutionError(
                    f"Skill '{manifest.tool_name}' timed out after {timeout}s"
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            if proc.returncode != 0:
                logger.warning(
                    "skill_execution_nonzero",
                    tool_name=manifest.tool_name,
                    returncode=proc.returncode,
                    stderr=stderr[:500],
                )
                return {
                    "error": f"Skill exited with code {proc.returncode}: {stderr[:500]}",
                    "stdout": stdout[:1000],
                    "exit_code": proc.returncode,
                }

            return self._parse_skill_output(
                {"stdout": stdout, "stderr": stderr, "exit_code": proc.returncode},
                tool_name=manifest.tool_name,
            )

        finally:
            # 清理临时文件
            try:
                os.unlink(params_file)
            except OSError:
                pass

    def _parse_skill_output(
        self,
        raw_result: dict[str, Any],
        tool_name: str,
    ) -> dict[str, Any]:
        """
        解析 Skill 的输出。

        Skill main.py 应输出有效 JSON 到 stdout，格式：
          {"result": {...}}  或  {"error": "..."}

        如果 stdout 为空或非 JSON，降级返回原始结果。
        """
        stdout = raw_result.get("stdout", "").strip()

        if stdout:
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    if "error" in parsed:
                        logger.warning(
                            "skill_returned_error",
                            tool_name=tool_name,
                            error=parsed["error"],
                        )
                    return parsed
            except json.JSONDecodeError:
                logger.warning(
                    "skill_stdout_not_json",
                    tool_name=tool_name,
                    preview=stdout[:200],
                )

        # 降级：返回原始结果
        exit_code = raw_result.get("exit_code", 0)
        return {
            "result": {
                "stdout": stdout,
                "stderr": raw_result.get("stderr", ""),
                "exit_code": exit_code,
            },
            "exit_code": exit_code,
        }


# ── 全局单例 ───────────────────────────────────────────────────

skill_adapter = SkillAdapter()
