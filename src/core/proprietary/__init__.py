"""
Core Proprietary Algorithms — 闭源核心算法包。

此包中的 .pyx 源码已编译为 .so 文件，不包含源码。
严禁反编译 .so 文件获取源码。

公开接口（从 src.services.* 公开调用）：
    from src.services.evolution import evolution_engine
    from src.services.etl import normalize, filter_sensitive, dedupe_content

内部模块（已被 stubs 替换）：
    from src.core.proprietary import ...  # 仅供 src.services.* 调用

导入路径（闭源）：
    from src.core.proprietary import normalize
"""

import os
import glob as _glob

# ── Safety: 确保 .so 编译产物存在 ────────────────────────────────

def _check_so() -> None:
    """验证至少有一个 .so 文件存在。"""
    so_pattern = os.path.join(os.path.dirname(__file__), "*.so")
    if not _glob.glob(so_pattern):
        raise RuntimeError(
            "Wisp proprietary modules not compiled. "
            "Please run: python scripts/compile_proprietary.py build"
        )

_check_so()

# ── Re-export from compiled modules ──────────────────────────────

from src.core.proprietary.etl import (
    normalize,
    filter_sensitive,
    dedupe_content,
    process,
)

from src.core.proprietary.evolution import (
    OutcomeRecord,
    SegmentAnalysis,
    PromptChange,
    EvolutionProposal,
    analyze_by_profile,
    bump_version,
    summarize_analyses,
    generate_proposal_changes,
)

__all__ = [
    # ETL
    "normalize",
    "filter_sensitive",
    "dedupe_content",
    "process",
    # Evolution
    "OutcomeRecord",
    "SegmentAnalysis",
    "PromptChange",
    "EvolutionProposal",
    "analyze_by_profile",
    "bump_version",
    "summarize_analyses",
    "generate_proposal_changes",
]
