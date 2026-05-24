"""
Wisp Skill 兼容层。

将第三方 / OpenClaw 格式的 Skill 适配为 Wisp Virtual Tool。

核心原则：
- 不修改原有 ToolRegistry 的内部实现
- Skill 以标准 Tool Schema 注册到全局 registry
- Skill 执行通过沙箱隔离，资源限制由 manifest.yaml 声明
- Wisp 启动时自动发现并注册所有合法 Skill
"""

from src.core.skills.registry import SkillRegistry, load_skills

__all__ = ["SkillRegistry", "load_skills"]
