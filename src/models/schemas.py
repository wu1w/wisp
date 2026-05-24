"""Pydantic 请求/响应模型。"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):  # noqa: UP042
    """任务状态枚举。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    HIBERNATING = "hibernating"


class TaskCreate(BaseModel):
    """创建任务的请求模型。"""

    description: str = Field(..., max_length=5000)
    user_id: str = Field(..., max_length=100)
    max_tool_calls: int = Field(default=50, ge=1, le=200)


class TaskResponse(BaseModel):
    """任务响应模型。"""

    id: UUID
    user_id: str
    description: str
    status: TaskStatus
    current_state: str
    tool_call_count: int
    max_tool_calls: int
    created_at: datetime
    updated_at: datetime


class StepStatus(str, Enum):  # noqa: UP042
    """Step 状态枚举。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class ToolExecutionCreate(BaseModel):
    """工具执行记录。"""

    task_id: UUID
    tool_name: str
    input_args: dict[str, Any]


class FileUploadResponse(BaseModel):
    """文件上传响应。"""

    file_id: UUID
    object_key: str
    status: str = "uploaded"


class FileMetadata(BaseModel):
    """文件元数据。"""

    id: UUID
    object_key: str
    filename: str
    mime_type: str | None
    size_bytes: int
    sha256: str | None
    is_verified: bool
    task_id: UUID | None
    created_at: datetime


# ── LLM 通用数据模型（Wisp 适配层）──────────────────────────────

class LLMMessage(BaseModel):
    """与供应商无关的统一消息格式。"""
    role: str  # system | user | assistant | tool
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    """与供应商无关的统一响应格式。"""
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    usage: dict[str, int] = Field(default_factory=dict)  # prompt_tokens, completion_tokens
    model: str  # 实际使用的模型名
    provider: str  # 供应商标识
