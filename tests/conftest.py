"""
Pytest 全局 Fixtures（无 DB 依赖）。

tests/test_config.py 等轻量测试使用此 conftest。
需要 DB/Redis/MinIO 的集成测试使用 tests/test_integration/conftest.py。
"""

from __future__ import annotations


