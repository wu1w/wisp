"""
Dreaming — 知识蒸馏模块（认知层）。

与 ETL Pipeline 并行：
- ETL：数据清洗（物理层），实时处理单条记忆
- Dreaming：知识蒸馏（认知层），离线处理记忆集合，提炼规则

安全约束：
- 默认状态为 pending_review，未经人类审批不得生效
- 必须使用 profiles.cheap 模型（成本控制）
- 输出必须比输入短得多（熵减检查）
"""

from src.core.dreaming.validator import DreamValidator
from src.core.dreaming.worker import DreamWorker

__all__ = ["DreamWorker", "DreamValidator"]
