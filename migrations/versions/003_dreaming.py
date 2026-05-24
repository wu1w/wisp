"""dreaming knowledge base tables

Revision ID: 003
Revises: 002
Create Date: 2026-05-23 22:31:00 UTC

架构说明：
- knowledge_base: 存储 Dreaming 提炼出的工程规则（pending_review 状态）
- dream_runs: 存储每次 Dreaming 运行的元数据（用于审计）

安全约束：
- 规则默认为 pending_review，未经人类审批不得被 Agent 使用
- 溯源字段 evidence_ids 保留原始记忆 ID，支持回溯
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── knowledge_base: 梦境产出规则表 ──────────────────────────
    op.create_table(
        "knowledge_base",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # 提炼出的规则文本（最核心字段）
        sa.Column("rule", sa.Text(), nullable=False),
        # 规则类别（用于分类检索）
        sa.Column(
            "category",
            sa.String(20),
            nullable=False,
            server_default="general",
        ),
        # 支撑这条规则的原始记忆 ID 列表（溯源）
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        # 置信度 0.0-1.0（由 LLM 评估）
        sa.Column("confidence", sa.Float(), nullable=True, server_default="0.5"),
        # 规则状态：pending_review | approved | rejected | applied
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending_review",
        ),
        # 审批信息
        sa.Column("approved_by", sa.String(100), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        # 审批时的人类备注
        sa.Column("human_note", sa.Text(), nullable=True),
        # 统计字段
        sa.Column("apply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reject_count", sa.Integer(), nullable=False, server_default="0"),
        # 时间戳
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_knowledge_base_status", "knowledge_base", ["status"])
    op.create_index("ix_knowledge_base_category", "knowledge_base", ["category"])
    op.create_index("ix_knowledge_base_confidence", "knowledge_base", ["confidence"])
    op.create_index("ix_knowledge_base_created_at", "knowledge_base", ["created_at"])

    # ── dream_runs: 梦境运行日志表 ────────────────────────────
    op.create_table(
        "dream_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("memory_count", sa.Integer(), nullable=False),
        sa.Column("rule_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        # 压缩比 = input_tokens / output_tokens（熵减指标）
        sa.Column("compression_ratio", sa.Float(), nullable=False),
        # 运行状态：success | no_rules | failed
        sa.Column("status", sa.String(20), nullable=False),
        # 关联的规则 ID 列表
        sa.Column(
            "rule_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        # 错误信息（如果失败）
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index("ix_dream_runs_created_at", "dream_runs", ["created_at"])
    op.create_index("ix_dream_runs_status", "dream_runs", ["status"])


def downgrade() -> None:
    op.drop_table("dream_runs")
    op.drop_table("knowledge_base")
