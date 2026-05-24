"""SQLAlchemy ORM 表定义（DDL 与 agent-design.md 保持一致）。"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有表的基类。"""

    pass


class Memory(Base):
    """记忆主表（pgvector 向量 + JSONB 元数据）。"""

    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSONB, nullable=True)
    extra: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    task_id: Mapped[UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Task(Base):
    """任务表。"""

    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running")
    current_state: Mapped[str] = mapped_column(String(30), default="IDLE")
    variable_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    max_tool_calls: Mapped[int] = mapped_column(Integer, default=50)
    prompt_version: Mapped[str] = mapped_column(String(20), default="v1.0.0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class File(Base):
    """文件注册表（对象存储元数据）。"""

    __tablename__ = "files"

    id: Mapped[UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    object_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    magic_number: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sandbox_only: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    task_id: Mapped[UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_files_task_id", "task_id"),
        Index("ix_files_object_key", "object_key"),
    )
