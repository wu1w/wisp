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
import sys
import glob as _glob

# ── Safety: 确保 .so 编译产物存在（Linux/macOS only）───────────────────────────

def _check_so() -> None:
    """Verify .so compiled artifacts exist. No-op on Windows."""
    if sys.platform == "win32":
        return  # Windows uses open-source Python stubs instead of .so
    so_pattern = os.path.join(os.path.dirname(__file__), "*.so")
    if not _glob.glob(so_pattern):
        raise RuntimeError(
            "Wisp proprietary modules not compiled. "
            "Please run: python scripts/compile_proprietary.py build"
        )

_check_so()

# ── Conditional import: use .so on Linux, stubs on Windows ─────────────────────
# On Windows, src/core/proprietary/ contains no .so files.
# The re-exported symbols are provided by src/services/etl.py and
# src/services/evolution.py which import from here.
# We provide no-op fallbacks so imports don't fail on Windows.

if sys.platform == "win32":
    # Provide dummy bindings so the import chain doesn't break on Windows.
    # The actual logic lives in src/services/*.py (open-source stubs).
    def _win32_stub(*args, **kwargs):
        raise NotImplementedError(
            "Proprietary module called on Windows — this should not happen. "
            "Use the open-source stubs in src/services/ instead."
        )

    # Stub out the ETL functions
    normalize = _win32_stub
    filter_sensitive = _win32_stub
    dedupe_content = _win32_stub
    process = _win32_stub

    # Stub out the Evolution types / functions
    class OutcomeRecord:
        _stub = True
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("Proprietary module called on Windows")
    class SegmentAnalysis:
        _stub = True
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("Proprietary module called on Windows")
    class PromptChange:
        _stub = True
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("Proprietary module called on Windows")
    class EvolutionProposal:
        _stub = True
        def __init__(self, *args, **kwargs):
            raise NotImplementedError("Proprietary module called on Windows")

    analyze_by_profile = _win32_stub
    bump_version = _win32_stub
    summarize_analyses = _win32_stub
    generate_proposal_changes = _win32_stub

else:
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
