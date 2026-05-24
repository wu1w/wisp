"""
ETL 流水线 — 公开接口（Stub）。

实际实现已编译到 src/core/proprietary/etl.cpython-*.so
本文 件仅重新导出公开接口，不包含算法逻辑。

勿直接修改本文 件——所有修改需在
src/core/proprietary/etl.pyx 中进行，然后重新编译。
"""

from src.core.proprietary import (
    normalize,
    filter_sensitive,
    dedupe_content,
    process,
)

__all__ = ["normalize", "filter_sensitive", "dedupe_content", "process"]
