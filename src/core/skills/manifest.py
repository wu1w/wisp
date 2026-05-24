"""
Skill Manifest — OpenClaw/Hermes 格式的 Skill 配置模型。

每个 Skill 必须包含 manifest.yaml，定义元数据、Tool Schema 和资源限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)


class SkillManifestError(Exception):
    """Manifest 解析或验证错误。"""
    pass


class SkillManifest:
    """
    Skill 配置清单。

    对应 skills/<skill_name>/manifest.yaml 的结构。
    """

    REQUIRED_FIELDS = ("name", "version", "description", "tool_schema")

    def __init__(
        self,
        name: str,
        version: str,
        description: str,
        tool_schema: dict[str, Any],
        author: str | None = None,
        runtime: dict[str, Any] | None = None,
        limits: dict[str, Any] | None = None,
        skill_path: Path | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.description = description
        self.tool_schema = tool_schema
        self.author = author
        self.runtime = runtime or {}
        self.limits = limits or {}
        self.skill_path = skill_path

        # 派生出 tool_name（来自 tool_schema.name）
        self.tool_name: str = tool_schema.get("name", "")
        if not self.tool_name:
            raise SkillManifestError("tool_schema.name is required")

    @classmethod
    def from_yaml(cls, skill_path: Path) -> SkillManifest:
        """
        从 skills/<skill_name>/manifest.yaml 加载并验证。

        验证规则：
        - manifest.yaml 必须存在
        - 必须包含 name, version, description, tool_schema
        - tool_schema.name 不能与现有内置工具重名
        """
        manifest_path = skill_path / "manifest.yaml"
        if not manifest_path.exists():
            raise SkillManifestError(f"manifest.yaml not found at {manifest_path}")

        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SkillManifestError(f"Invalid YAML in manifest.yaml: {exc}")

        if not isinstance(raw, dict):
            raise SkillManifestError("manifest.yaml must be a YAML object")

        missing = [f for f in cls.REQUIRED_FIELDS if f not in raw]
        if missing:
            raise SkillManifestError(f"Missing required fields: {missing}")

        # tool_schema 本身也必须是一个 dict
        if not isinstance(raw.get("tool_schema"), dict):
            raise SkillManifestError("tool_schema must be an object")

        # 验证 tool_schema.name 格式（必须符合 Python identifier + OpenAI tool name）
        tool_name = raw["tool_schema"].get("name", "")
        if not tool_name or not isinstance(tool_name, str):
            raise SkillManifestError("tool_schema.name is required and must be a string")

        return cls(
            name=str(raw["name"]),
            version=str(raw["version"]),
            description=str(raw["description"]),
            tool_schema=raw["tool_schema"],
            author=raw.get("author"),
            runtime=raw.get("runtime"),
            limits=raw.get("limits", {}),
            skill_path=skill_path,
        )

    # ── 资源限制属性 ─────────────────────────────────────────

    @property
    def timeout_seconds(self) -> int:
        """执行超时（秒）。"""
        return int(self.limits.get("timeout", 60))

    @property
    def allow_network(self) -> bool:
        """是否允许网络访问。"""
        return bool(self.limits.get("network", False))

    @property
    def memory_limit(self) -> str:
        """内存限制字符串（如 '1g'）。"""
        return str(self.limits.get("memory", "512m"))

    @property
    def python_version(self) -> str:
        """运行时 Python 版本。"""
        return self.runtime.get("python_version", "3.11") if self.runtime else "3.11"

    @property
    def requirements_path(self) -> Path | None:
        """requirements.txt 路径（如果存在）。"""
        if self.skill_path:
            req = self.skill_path / "requirements.txt"
            return req if req.exists() else None
        return None

    def __repr__(self) -> str:
        return f"SkillManifest(name={self.name!r}, tool={self.tool_name!r}, version={self.version!r})"
