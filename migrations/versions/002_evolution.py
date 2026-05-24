"""evolution engine tables

Revision ID: 002
Revises: 001
Create Date: 2026-05-23 20:30:00 UTC

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── outcome records ────────────────────────────────────────
    op.create_table(
        "evolution_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile", sa.String(20), nullable=False),  # coding | chatting | cheap
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("tool_calls", sa.Integer, nullable=False),
        sa.Column("error_count", sa.Integer, nullable=False),
        sa.Column("reflection_summary", sa.Text, nullable=True),
        sa.Column("latency_seconds", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_evolution_outcomes_task_id", "evolution_outcomes", ["task_id"])
    op.create_index("ix_evolution_outcomes_profile", "evolution_outcomes", ["profile"])
    op.create_index("ix_evolution_outcomes_created_at", "evolution_outcomes", ["created_at"])

    # ── evolution proposals ──────────────────────────────────
    op.create_table(
        "evolution_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_id", sa.String(50), nullable=False, unique=True),
        sa.Column("target_version", sa.String(20), nullable=False),  # e.g. v2.0
        sa.Column("new_version", sa.String(20), nullable=False),       # e.g. v2.1
        sa.Column("analyses_json", postgresql.JSONB, nullable=False),  # list of SegmentAnalysis dicts
        sa.Column("changes_json", postgresql.JSONB, nullable=False),   # list of PromptChange dicts
        sa.Column("summary", sa.Text, nullable=False),                # LLM-generated summary
        sa.Column("approved", sa.Boolean, nullable=True, default=None),  # null=pending, true=approved, false=rejected
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_evolution_proposals_approved", "evolution_proposals", ["approved"])
    op.create_index("ix_evolution_proposals_created_at", "evolution_proposals", ["created_at"])


def downgrade() -> None:
    op.drop_table("evolution_proposals")
    op.drop_table("evolution_outcomes")
