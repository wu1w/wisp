"""
Skill Registry — 自动发现并注册 Skill 为 Wisp Virtual Tool。

启动流程：
  1. 扫描 skills/ 目录下所有子目录
  2. 每个子目录读取 manifest.yaml，验证合法性
  3. 将 Skill 注册到全局 ToolRegistry（作为标准 Tool）
  4. 记录已安装的依赖，供后续安装

安全约束：
- 禁止 skill_name 与内置工具名冲突
- 网络访问受 manifest.network 限制
- 依赖安装仅在首次加载时触发
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.core.skills.manifest import SkillManifest, SkillManifestError
from src.utils.config import get_config

logger = structlog.get_logger(__name__)

# 内置工具名（禁止冲突）
_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset([
    "read_file",
    "write_file",
    "list_dir",
    "execute_in_sandbox",
    "search_memory",
    "reflect_on_error",
    "save_memory",
    "bash",
])


class SkillRegistry:
    """
    Skill 注册中心。

    职责：
    - 扫描 skills/ 目录，发现所有合法 Skill
    - 验证 manifest.yaml 合法性
    - 注册为 Tool（注册到全局 ToolRegistry）
    - 管理 Skill 的生命周期（加载/卸载）
    """

    def __init__(
        self,
        skills_dir: str | Path | None = None,
    ) -> None:
        config = get_config()
        self._skills_dir: Path = (
            Path(skills_dir)
            if skills_dir
            else Path(config.get("skills_dir", "skills"))
        )
        self._registered_skills: dict[str, SkillManifest] = {}
        self._loaded: bool = False

    @property
    def skills_dir(self) -> Path:
        return self._skills_dir

    @property
    def skills(self) -> dict[str, SkillManifest]:
        """已注册的 Skill 字典（skill_name → manifest）。"""
        return self._registered_skills

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def discover_skills(self) -> dict[str, SkillManifest]:
        """
        扫描 skills/ 目录，注册所有合法的 Skill。

        返回：{tool_name: SkillManifest}
        """

        if not self._skills_dir.exists():
            logger.warning("skills_dir_not_found", path=str(self._skills_dir))
            self._loaded = True
            return {}

        for skill_path in self._skills_dir.iterdir():
            if not skill_path.is_dir():
                continue

            try:
                manifest = SkillManifest.from_yaml(skill_path)
            except SkillManifestError as exc:
                logger.warning(
                    "skill_manifest_invalid_skipped",
                    skill_path=str(skill_path),
                    reason=str(exc),
                )
                continue

            tool_name = manifest.tool_name

            # 冲突检测
            if tool_name in _BUILTIN_TOOL_NAMES:
                logger.warning(
                    "skill_name_conflict_skipped",
                    skill=manifest.name,
                    tool_name=tool_name,
                )
                continue

            if tool_name in self._registered_skills:
                logger.warning(
                    "skill_duplicate_skipped",
                    skill=manifest.name,
                    tool_name=tool_name,
                )
                continue

            # 注册到全局 ToolRegistry（作为 Virtual Tool）
            self._register_as_tool(manifest)

            # 安装依赖（如需要）
            self._install_dependencies(manifest)

            self._registered_skills[tool_name] = manifest
            logger.info(
                "skill_registered",
                skill=manifest.name,
                tool_name=tool_name,
                version=manifest.version,
                network_allowed=manifest.allow_network,
                timeout_s=manifest.timeout_seconds,
            )

        self._loaded = True
        logger.info(
            "skills_discovery_complete",
            total=len(self._registered_skills),
            skills=list(self._registered_skills.keys()),
        )
        return self._registered_skills

    def _register_as_tool(self, manifest: SkillManifest) -> None:
        """
        将 Skill 注册为全局 Tool。

        Skill 的 tool_schema 直接对应 OpenAI Function Calling 格式。
        handler 统一路由到 SkillAdapter。
        """
        from src.core.skills.adapter import skill_adapter

        # 导入全局 registry
        from src.core.tools import registry as tool_registry

        tool_name = manifest.tool_name

        # 注册 tool schema
        tool_registry.register(
            name=tool_name,
            description=manifest.description,
            parameters=manifest.tool_schema.get("parameters", {}),
            handler=skill_adapter.create_handler(manifest),
        )
        logger.debug(
            "skill_tool_registered",
            tool_name=tool_name,
            skill=manifest.name,
        )

    def _install_dependencies(self, manifest: SkillManifest) -> None:
        """
        安装 Skill 的依赖（首次加载时）。

        如果 requirements.txt 存在，在后台线程执行 pip install。
        失败不影响 Skill 注册（依赖缺失会在执行时报错）。
        """
        req_path = manifest.requirements_path
        if req_path is None or not req_path.exists():
            return

        import subprocess
        import threading

        def _pip_install() -> None:
            try:
                subprocess.run(
                    ["pip", "install", "-r", str(req_path), "--quiet"],
                    capture_output=True,
                    timeout=120,
                )
                logger.info("skill_deps_installed", skill=manifest.name, req=str(req_path))
            except Exception as exc:
                logger.warning(
                    "skill_deps_install_failed",
                    skill=manifest.name,
                    req=str(req_path),
                    error=str(exc),
                )

        thread = threading.Thread(target=_pip_install, daemon=True)
        thread.start()

    def get_skill(self, tool_name: str) -> SkillManifest | None:
        """根据 tool_name 查找 Skill manifest。"""
        return self._registered_skills.get(tool_name)

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """
        返回所有已注册 Skill 的 tool_schema 列表。

        用于 LLM 的 tools 参数注入。
        """
        return [
            manifest.tool_schema
            for manifest in self._registered_skills.values()
        ]

    def skill_names(self) -> list[str]:
        """返回所有已注册 Skill 的 tool_name 列表。"""
        return list(self._registered_skills.keys())


# ── 全局单例 + 自动发现 ───────────────────────────────────────

# 延迟初始化：模块导入时不执行，load_skills() 显式调用
_skill_registry: SkillRegistry | None = None


def get_skill_registry() -> SkillRegistry:
    """获取全局 SkillRegistry 单例。"""
    global _skill_registry
    if _skill_registry is None:
        _skill_registry = SkillRegistry()
    return _skill_registry


def load_skills() -> dict[str, SkillManifest]:
    """
    启动时调用：扫描 skills/ 并注册所有合法 Skill。

    幂等：重复调用直接返回已有结果。
    """
    registry = get_skill_registry()
    if registry.is_loaded:
        return registry.skills
    return registry.discover_skills()
