"""initial migration: create all tables

Revision ID: 001
Revises:
Create Date: 2026-05-22 16:35:00 UTC

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── memories ────────────────────────────────────────────────
    op.create_table(
        'memories',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('type', sa.String(20), nullable=False),
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('embedding', postgresql.JSONB, nullable=True),
        sa.Column('metadata', postgresql.JSONB, server_default='{}'),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('user_id', sa.String(100), nullable=True),
        sa.Column('success', sa.Boolean, nullable=True),
        sa.Column('tool_name', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # pgvector 扩展需单独安装，暂时用普通 Gin 索引替代
    op.create_index('ix_memories_task_id', 'memories', ['task_id'])
    op.create_index('ix_memories_user_id', 'memories', ['user_id'])

    # ── tasks ────────────────────────────────────────────────
    op.create_table(
        'tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', sa.String(100), nullable=False),
        sa.Column('description', sa.Text, nullable=False),
        sa.Column('status', sa.String(20), server_default='running'),
        sa.Column('current_state', sa.String(30), server_default='IDLE'),
        sa.Column('variable_context', postgresql.JSONB, server_default='{}'),
        sa.Column('tool_call_count', sa.Integer, server_default='0'),
        sa.Column('max_tool_calls', sa.Integer, server_default='50'),
        sa.Column('prompt_version', sa.String(20), server_default='v1.0.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_tasks_status', 'tasks', ['status'])
    op.create_index('ix_tasks_user_id', 'tasks', ['user_id'])

    # ── tool_executions ────────────────────────────────────────
    op.create_table(
        'tool_executions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id', ondelete='CASCADE')),
        sa.Column('seq', sa.Integer, nullable=False),
        sa.Column('tool_name', sa.String(50), nullable=False),
        sa.Column('input_args', postgresql.JSONB, nullable=False),
        sa.Column('output', postgresql.JSONB),
        sa.Column('error', sa.Text),
        sa.Column('exit_code', sa.Integer),
        sa.Column('duration_ms', sa.Integer),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_tool_executions_task_seq', 'tool_executions', ['task_id', sa.text('seq DESC')])

    # ── prompt_versions ────────────────────────────────────────
    op.create_table(
        'prompt_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('version', sa.String(20), nullable=False, unique=True),
        sa.Column('system_prompt', sa.Text, nullable=False),
        sa.Column('change_summary', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('is_active', sa.Boolean, server_default='false'),
        sa.Column('created_by', sa.String(100)),
    )

    # ── reflection_reports ────────────────────────────────────
    op.create_table(
        'reflection_reports',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id', ondelete='CASCADE')),
        sa.Column('error_type', sa.String(100)),
        sa.Column('root_cause', sa.Text),
        sa.Column('fix_suggestion', sa.Text),
        sa.Column('prompt_delta', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_reflection_reports_task_id', 'reflection_reports', ['task_id'])

    # ── users ──────────────────────────────────────────────────
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('username', sa.String(100), nullable=False, unique=True),
        sa.Column('password_hash', sa.String(255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── task_steps ────────────────────────────────────────────
    op.create_table(
        'task_steps',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('seq', sa.Integer, nullable=False),
        sa.Column('state', sa.String(20), server_default='pending'),
        sa.Column('tool_name', sa.String(50)),
        sa.Column('input_args', postgresql.JSONB),
        sa.Column('output', postgresql.JSONB),
        sa.Column('error', sa.Text),
        sa.Column('attempt', sa.Integer, server_default='1'),
        sa.Column('max_attempts', sa.Integer, server_default='3'),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True)),
        sa.Column('ttl_seconds', sa.Integer, server_default='300'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_task_steps_task_seq', 'task_steps', ['task_id', 'seq'])
    op.create_index('ix_task_steps_state_heartbeat', 'task_steps',
                    ['state', 'heartbeat_at'],
                    postgresql_where=sa.text("state = 'running'"))

    # ── agent_checkpoints ─────────────────────────────────────
    op.create_table(
        'agent_checkpoints',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id', ondelete='CASCADE')),
        sa.Column('step_seq', sa.Integer, server_default='0'),
        sa.Column('workflow_state', postgresql.JSONB, server_default='{}'),
        sa.Column('messages', postgresql.JSONB, server_default='[]'),
        sa.Column('core_facts', postgresql.JSONB, server_default='{}'),
        sa.Column('variable_context', postgresql.JSONB, server_default='{}'),
        sa.Column('pending_steps', postgresql.JSONB, server_default='[]'),
        sa.Column('prompt_version', sa.String(20)),
        sa.Column('is_active', sa.Boolean, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_agent_checkpoints_task_seq', 'agent_checkpoints', ['task_id', sa.text('step_seq DESC')])
    op.create_index('ix_agent_checkpoints_task_active',
                    'agent_checkpoints', ['task_id'],
                    postgresql_where=sa.text("is_active = true",),
                    unique=True)

    # ── agent_state_snapshots ─────────────────────────────────
    op.create_table(
        'agent_state_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id'), unique=True),
        sa.Column('state_data', postgresql.JSONB, nullable=False),
        sa.Column('hibernate_reason', sa.String(100)),
        sa.Column('is_active', sa.Boolean, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ── dead_letter_queue ─────────────────────────────────────
    op.create_table(
        'dead_letter_queue',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('step_id', postgresql.UUID(as_uuid=True)),
        sa.Column('tool_name', sa.String(50)),
        sa.Column('input_args', postgresql.JSONB),
        sa.Column('error', sa.Text),
        sa.Column('attempt_count', sa.Integer),
        sa.Column('resolved', sa.Boolean, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_dead_letter_queue_resolved', 'dead_letter_queue', ['resolved'])

    # ── traces ────────────────────────────────────────────────
    op.create_table(
        'traces',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('user_id', sa.String(100)),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('ended_at', sa.DateTime(timezone=True)),
        sa.Column('total_tokens', sa.Integer, server_default='0'),
        sa.Column('total_cost_usd', sa.Numeric(10, 6), server_default='0'),
        sa.Column('status', sa.String(20), server_default='running'),
    )
    op.create_index('ix_traces_task_id', 'traces', ['task_id'])
    op.create_index('ix_traces_status_started', 'traces', ['status', 'started_at'])

    # ── spans ─────────────────────────────────────────────────
    op.create_table(
        'spans',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('trace_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('parent_span_id', postgresql.UUID(as_uuid=True)),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('span_type', sa.String(20), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('ended_at', sa.DateTime(timezone=True)),
        sa.Column('duration_ms', sa.Integer),
        sa.Column('input_tokens', sa.Integer),
        sa.Column('output_tokens', sa.Integer),
        sa.Column('cost_usd', sa.Numeric(10, 6)),
        sa.Column('input_args', postgresql.JSONB),
        sa.Column('output', postgresql.JSONB),
        sa.Column('error', sa.Text),
    )
    op.create_index('ix_spans_trace_id', 'spans', ['trace_id'])
    op.create_index('ix_spans_parent_span_id', 'spans', ['parent_span_id'])
    op.create_index('ix_spans_type_duration',
                    'spans', ['span_type', 'duration_ms'],
                    postgresql_where=sa.text("span_type = 'llm'"))

    # ── file_versions ──────────────────────────────────────────
    op.create_table(
        'file_versions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('file_path', sa.Text, nullable=False),
        sa.Column('versioned_path', sa.Text, nullable=False),
        sa.Column('version_tag', sa.String(20), nullable=False),
        sa.Column('version_seq', sa.Integer, nullable=False),
        sa.Column('commit_hash', sa.String(40)),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_file_versions_path_seq', 'file_versions', ['file_path', sa.text('version_seq DESC')])
    op.create_index('ix_file_versions_task_id', 'file_versions', ['task_id'])

    # ── approval_requests ───────────────────────────────────────
    op.create_table(
        'approval_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('action', sa.String(50), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('payload', postgresql.JSONB),
        sa.Column('severity', sa.String(10), server_default='medium'),
        sa.Column('status', sa.String(20), server_default='pending'),
        sa.Column('approver_comment', sa.Text),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True),
                  server_default=sa.text("(NOW() + INTERVAL '24 hours')")),
        sa.Column('responded_at', sa.DateTime(timezone=True)),
    )
    op.create_index('ix_approval_requests_task_status', 'approval_requests', ['task_id', 'status'])
    op.create_index('ix_approval_requests_status_expires',
                    'approval_requests', ['status', 'expires_at'],
                    postgresql_where=sa.text("status = 'pending'"))

    # ── task_steer_events ──────────────────────────────────────
    op.create_table(
        'task_steer_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('message', sa.Text, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_task_steer_events_task_id', 'task_steer_events', ['task_id'])

    # ── files ─────────────────────────────────────────────────
    op.create_table(
        'files',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('object_key', sa.Text, nullable=False, unique=True),
        sa.Column('filename', sa.Text, nullable=False),
        sa.Column('mime_type', sa.String(100)),
        sa.Column('size_bytes', sa.BigInteger, server_default='0'),
        sa.Column('sha256', sa.String(64)),
        sa.Column('magic_number', sa.String(8)),
        sa.Column('is_verified', sa.Boolean, server_default='false'),
        sa.Column('is_sandbox_only', sa.Boolean, server_default='false'),
        sa.Column('uploaded_by', sa.String(100)),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_files_task_id', 'files', ['task_id'])
    op.create_index('ix_files_object_key', 'files', ['object_key'])


def downgrade() -> None:
    """删除所有表（严格按依赖顺序）。"""
    op.drop_table('files')
    op.drop_table('task_steer_events')
    op.drop_table('approval_requests')
    op.drop_table('file_versions')
    op.drop_table('spans')
    op.drop_table('traces')
    op.drop_table('dead_letter_queue')
    op.drop_table('agent_state_snapshots')
    op.drop_table('agent_checkpoints')
    op.drop_table('task_steps')
    op.drop_table('users')
    op.drop_table('reflection_reports')
    op.drop_table('prompt_versions')
    op.drop_table('tool_executions')
    op.drop_table('tasks')
    op.drop_table('memories')
