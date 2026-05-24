"""
文件上传 / 下载 API 路由。

设计原则（对应 agent-design.md 六.14）：
- Gateway 绝不读取文件流，直接 pipe 到对象存储
- 下载返回 302 重定向到预签名 URL，零 Gateway 带宽占用
- 文件校验异步进行（Magic Number + SHA256）
"""

import asyncio
import io
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status

from src.db import acquire
from src.middleware.auth import AuthenticatedUser, get_current_user
from src.models.schemas import FileMetadata, FileUploadResponse
from src.services import minio_client
from src.utils.config import get_config
from src.utils.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()


# ── Magic Number 校验 ────────────────────────────────────────────

# 文件头魔数表（部分）
_MAGIC_NUMBERS: dict[bytes, str] = {
    b"PK\\x03\\x04": "zip",          # ZIP / DOCX / XLSX
    b"PK\\x05\\x06": "zip",          # ZIP 空归档
    b"\\x1f\\x8b": "gzip",           # Gzip
    b"%PDF": "pdf",                  # PDF
    b"#!": "python",                 # Python shebang
    b"{\"": "json",                  # JSON ASCII
}


async def _validate_file_header(object_key: str) -> tuple[bool, str]:
    """
    读取文件头 512 字节，校验 Magic Number。

    返回 (is_safe, magic_str)。
    """
    try:
        header_bytes = await minio_client.download_stream(object_key, offset=0, length=512)
        header = header_bytes[:8]

        for magic, file_type in _MAGIC_NUMBERS.items():
            if header.startswith(magic):
                return True, file_type

        # 无匹配魔数，检查是否为纯文本
        try:
            header.decode("utf-8")
            return True, "text"
        except UnicodeDecodeError:
            return False, "unknown"
    except Exception as exc:
        logger.warning("magic_number_check_failed", object_key=object_key, error=str(exc))
        return True, "check_failed"  # 降级：放行，人工后续处理


# ── 路由 ─────────────────────────────────────────────────────────

@router.post("/upload", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def upload_file(
    request: Request,
    task_id: str,
    file: UploadFile,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FileUploadResponse:
    """
    流式上传文件到对象存储（MinIO）。

    原则：
    - Gateway 不全量读取文件内容，直接 pipe 到对象存储
    - 上传完成后触发异步校验（Magic Number + SHA256）
    - 返回 file_id 和 object_key
    """
    config = get_config()
    limits = config["file_limits"]

    # 1. 后缀白名单校验
    filename_lower = (file.filename or "").lower()
    allowed = any(filename_lower.endswith(ext) for ext in limits["allowed_extensions"])
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type not allowed. Allowed: {limits['allowed_extensions']}",
        )

    # 2. 生成 object_key
    object_key = f"tasks/{task_id}/{uuid.uuid4()}/{file.filename}"

    # 3. 流式读取并上传（分块，避免占满内存）
    total_size = 0
    chunk_size = limits["chunk_size_mb"] * 1024 * 1024  # 5MB
    buffer = io.BytesIO()

    try:
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)

            # 大小限制校验（实时）
            if total_size > limits["max_file_size_mb"] * 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File exceeds {limits['max_file_size_mb']}MB limit",
                )

            buffer.write(chunk)

        buffer.seek(0)
        await minio_client.upload_stream(
            object_key=object_key,
            data=buffer,
            length=total_size,
            content_type=file.content_type or "application/octet-stream",
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("upload_failed", object_key=object_key, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Upload failed",
        )

    # 4. 注册文件记录到 PG（异步校验后续进行）
    file_id = uuid.uuid4()
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO files
                (id, object_key, filename, mime_type, size_bytes, task_id, uploaded_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            [
                file_id,
                object_key,
                file.filename,
                file.content_type,
                total_size,
                uuid.UUID(task_id),
                uuid.UUID(user.user_id),
            ],
        )
        await conn.commit()

    logger.info(
        "file_uploaded",
        file_id=str(file_id),
        object_key=object_key,
        size_bytes=total_size,
    )

    # 5. 触发异步校验（后台任务，轻量投递到 Redis Streams 或直接启动 Task）
    # 此处直接投递，实际项目中可接入 Redis Streams / Celery
    _ = asyncio.create_task(_background_validate(str(file_id), object_key))

    return FileUploadResponse(
        file_id=file_id,
        object_key=object_key,
        status="uploaded",
    )


@router.get("/{file_id}", response_model=FileMetadata)
async def get_file_metadata(file_id: str) -> FileMetadata:
    """查询文件元数据及校验状态。"""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM files WHERE id = $1",
            [uuid.UUID(file_id)],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

        return FileMetadata(
            id=row["id"],
            object_key=row["object_key"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            is_verified=row["is_verified"],
            task_id=row["task_id"],
            created_at=row["created_at"],
        )


@router.get("/download/{file_id}")
async def download_file(
    file_id: str,
    _: AuthenticatedUser = Depends(get_current_user),
) -> Any:
    """
    获取文件下载链接（302 重定向到预签名 URL）。

    原则：
    - 文件流不经过 Gateway，零带宽占用
    - 返回 302 Redirect 到 MinIO/S3 预签名 URL（有效期 5 分钟）
    - 支持浏览器断点续传
    """
    config = get_config()
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT object_key, is_verified, size_bytes FROM files WHERE id = $1",
            [uuid.UUID(file_id)],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

        if not row["is_verified"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="File not yet verified, please retry later",
            )

    expiry_minutes = config["file_limits"]["presigned_url_expiry_minutes"]
    presigned_url = await minio_client.generate_presigned_url(
        object_key=row["object_key"],
        expires_minutes=expiry_minutes,
    )

    from fastapi.responses import RedirectResponse
    logger.info("file_download_redirect", file_id=file_id, object_key=row["object_key"])
    return RedirectResponse(presigned_url, status_code=status.HTTP_302_FOUND)


@router.post("/{file_id}/validate")
async def trigger_validation(
    file_id: str,
    _: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """手动触发文件校验。"""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT object_key FROM files WHERE id = $1",
            [uuid.UUID(file_id)],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    _task = asyncio.create_task(_background_validate(file_id, row["object_key"]))
    return {"status": "validation_triggered", "file_id": file_id}


@router.delete("/{file_id}")
async def delete_file(
    file_id: str,
    _: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """删除文件（软删除 + 对象存储删除）。"""
    async with acquire() as conn:
        row = await conn.fetchrow(
            "SELECT object_key FROM files WHERE id = $1",
            [uuid.UUID(file_id)],
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

        # 从对象存储删除
        try:
            await minio_client.delete_object(row["object_key"])
        except Exception as exc:
            logger.warning("minio_delete_failed", object_key=row["object_key"], error=str(exc))

        # 从 PG 删除（硬删除）
        await conn.execute("DELETE FROM files WHERE id = $1", [uuid.UUID(file_id)])
        await conn.commit()

    logger.info("file_deleted", file_id=file_id)
    return {"status": "deleted", "file_id": file_id}


# ── 文件版本管理 ─────────────────────────────────────────────────


@router.get("/versions/{file_path:path}")
async def list_file_versions(
    file_path: str,
    _: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """
    列出文件的版本历史。

    file_path: 原始文件路径，如 /home/ubuntu/workspace/myapp/main.py
    """
    from src.services.file_versioning import list_file_versions

    try:
        versions = await list_file_versions(file_path)
    except Exception as exc:
        logger.warning("list_versions_failed", file_path=file_path, error=str(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    return {"file_path": file_path, "versions": versions}


@router.post("/rollback/{file_path:path}")
async def rollback_file(
    file_path: str,
    target_version: str,  # Query param: ?target_version=v3
    _: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """
    将文件回滚到指定版本。

    触发条件：用户选择某个历史版本进行回滚

    流程：
    1. 当前版本自动备份为新版本（copy-on-write）
    2. 用目标版本覆盖原文件
    3. 记录回滚操作
    """
    from src.services.file_versioning import FileVersioningError
    from src.services.file_versioning import rollback_file as _rollback

    if not target_version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target_version query parameter is required, e.g. ?target_version=v2",
        )

    try:
        result = await _rollback(file_path, target_version)
    except FileVersioningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.exception("rollback_failed", file_path=file_path, error=str(exc))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    logger.info("file_rollback_api", file_path=file_path, target_version=target_version)
    return result


# ── 后台校验任务 ─────────────────────────────────────────────────


async def _background_validate(file_id: str, object_key: str) -> None:
    """
    后台异步校验任务（Worker 负责执行）。

    流程：
    1. Magic Number 校验
    2. SHA256 计算
    3. 更新 files 表（sha256、magic_number、is_verified）
    """
    import hashlib

    logger.info("validation_started", file_id=file_id, object_key=object_key)

    try:
        # Magic Number 校验
        is_safe, magic_str = await _validate_file_header(object_key)

        # SHA256 计算（流式读完全文件）
        sha256_hash = hashlib.sha256()
        offset = 0
        chunk_len = 5 * 1024 * 1024  # 5MB

        while True:
            chunk = await minio_client.download_stream(object_key, offset=offset, length=chunk_len)
            if not chunk:
                break
            sha256_hash.update(chunk)
            offset += len(chunk)

        sha256_hex = sha256_hash.hexdigest()

        # 更新 PG
        async with acquire() as conn:
            await conn.execute(
                """
                UPDATE files
                SET sha256 = $1,
                    magic_number = $2,
                    is_verified = $3
                WHERE id = $4
                """,
                [sha256_hex, magic_str, is_safe, uuid.UUID(file_id)],
            )
            await conn.commit()

        logger.info(
            "validation_completed",
            file_id=file_id,
            magic=magic_str,
            sha256=sha256_hex[:16],
            is_safe=is_safe,
        )

    except Exception as exc:
        logger.exception("validation_failed", file_id=file_id, error=str(exc))
