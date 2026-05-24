"""
MinIO / S3 对象存储客户端。

所有文件操作（上传、下载、预签名 URL）必须通过此类，
严禁直接调用 boto3 / minio SDK。
"""

import io

from minio import Minio
from minio.datatypes import Object

from src.utils.config import get_config

_client: Minio | None = None
_bucket: str = ""


def _get_client() -> Minio:
    if _client is None:
        raise RuntimeError("MinIO client not initialized. Call init_minio() first.")
    return _client


def init_minio() -> None:
    """初始化 MinIO 客户端（在应用启动时调用）。"""
    global _client, _bucket
    config = get_config()
    os_cfg = config["object_storage"]

    _client = Minio(
        endpoint=os_cfg["endpoint"],
        access_key=os_cfg["access_key"],
        secret_key=os_cfg["secret_key"],
        secure=os_cfg.get("secure", False),
    )
    _bucket = os_cfg["bucket"]

    # 确保 bucket 存在（优雅降级：MinIO 不可用时跳过）
    try:
        if not _client.bucket_exists(_bucket):
            _client.make_bucket(_bucket)
    except Exception:
        pass  # MinIO 不可用时继续启动，文件操作会延迟失败


# ── 文件操作 ──────────────────────────────────────────────────

async def upload_stream(
    object_key: str,
    data: io.BytesIO | io.BufferedReader,
    length: int | None = None,
    content_type: str = "application/octet-stream",
) -> None:
    """
    流式上传文件到对象存储。

    参数：
        object_key: 存储路径，如 'tasks/{task_id}/source_code.zip'
        data: 字节流对象
        length: 数据长度（-1 表示未知，分块传输）
        content_type: MIME 类型
    """
    client = _get_client()
    # minio SDK 本身是同步的，但我们在线程池执行，不阻塞事件循环
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: client.put_object(
            bucket_name=_bucket,
            object_name=object_key,
            data=data,
            length=length or -1,
            part_size=5 * 1024 * 1024,  # 5MB 分块
            content_type=content_type,
        ),
    )


async def download_stream(object_key: str, offset: int = 0, length: int = 0) -> bytes:
    """
    流式下载文件（指定偏移量和长度）。

    用于读取文件头（Magic Number 校验）。
    """
    client = _get_client()
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: client.get_object(
            bucket_name=_bucket,
            object_name=object_key,
            offset=offset,
            length=length,
        ).read(),
    )


async def generate_presigned_url(
    object_key: str,
    expires_minutes: int = 5,
) -> str:
    """
    生成预签名下载 URL（有效期默认 5 分钟）。

    用于 Gateway 返回 302 重定向到对象存储。
    """
    import datetime

    client = _get_client()
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: client.presigned_get_object(
            bucket_name=_bucket,
            object_name=object_key,
            expires=datetime.timedelta(minutes=expires_minutes),
        ),
    )


async def delete_object(object_key: str) -> None:
    """删除对象。"""
    client = _get_client()
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: client.remove_object(bucket_name=_bucket, object_name=object_key),
    )


async def stat_object(object_key: str) -> Object:
    """获取对象元数据。"""
    client = _get_client()
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: client.stat_object(bucket_name=_bucket, object_name=object_key),
    )
