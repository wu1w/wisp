"""
文件版本管理：Copy-on-Write + 一键回滚。

设计原则（对应 agent-design.md 六.12）：
- 严禁直接覆盖原文件，每次写入生成版本快照
- 版本快照存储在 .versions/ 目录下
- 支持 Git 提交（可选）
- Rollback API 支持回滚到任意版本
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from src.db import acquire
from src.utils.security import SecurityError, validate_path

logger = structlog.get_logger(__name__)

# 版本存储根目录
_VERSION_BASE = Path("/home/ubuntu/workspace/.versions")
# 允许版本化的路径前缀
_VERSION_ALLOWED_PREFIXES = ("/home/ubuntu/workspace/",)


class FileVersioningError(Exception):
    """版本管理错误。"""


def _is_versionable_path(path: str) -> bool:
    """判断路径是否允许版本化管理。"""
    try:
        p = validate_path(path, allow_create=False)
    except SecurityError:
        return False
    return any(str(p).startswith(prefix) for prefix in _VERSION_ALLOWED_PREFIXES)


def _resolve_version_path(original_path: str, version_seq: int, timestamp: str) -> Path:
    """生成版本化文件路径。"""
    p = Path(original_path)
    stem = p.stem
    suffix = p.suffix
    # 用相对路径拼接版本目录
    rel = str(p.parent).lstrip("/")  # e.g. "home/ubuntu/workspace"
    versioned_dir = _VERSION_BASE / rel
    versioned_dir.mkdir(parents=True, exist_ok=True)
    return versioned_dir / f"{stem}_v{version_seq}_{timestamp}{suffix}"


async def write_file_version(
    original_path: str,
    content: str,
    task_id: str | None = None,
    commit_hash: str | None = None,
) -> dict[str, Any]:
    """
    Copy-on-Write 版本写入。

    流程：
    1. 读取当前文件内容（如果存在）
    2. 查询最新版本号
    3. 生成版本快照路径，写入 .versions/
    4. 写入原文件
    5. 写入 file_versions 表

    返回：
        version_id, version_tag, versioned_path, bytes_written
    """
    if not _is_versionable_path(original_path):
        raise FileVersioningError(
            f"Path {original_path!r} is not under versionable paths: {_VERSION_ALLOWED_PREFIXES}"
        )

    # 查询当前最大版本号
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT MAX(version_seq) AS max_seq
            FROM file_versions
            WHERE file_path = $1
            """,
            [original_path],
        )
        max_seq = row["max_seq"] or 0

    new_seq = max_seq + 1
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    versioned_path = _resolve_version_path(original_path, new_seq, timestamp)

    # 如果原文件存在，复制到版本快照
    original_p = Path(original_path)
    if original_p.exists():
        shutil.copy2(original_p, versioned_path)

    # 写入新内容到原文件
    original_p.parent.mkdir(parents=True, exist_ok=True)
    original_p.write_text(content, encoding="utf-8")

    # 写入 PG 版本记录
    version_id = uuid.uuid4()
    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO file_versions
                (id, file_path, versioned_path, version_tag, version_seq,
                 commit_hash, task_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            [
                version_id,
                original_path,
                str(versioned_path),
                f"v{new_seq}",
                new_seq,
                commit_hash,
                uuid.UUID(task_id) if task_id else None,
            ],
        )
        await conn.commit()

    logger.info(
        "file_version_created",
        version_id=str(version_id),
        file_path=original_path,
        version_tag=f"v{new_seq}",
    )

    return {
        "version_id": str(version_id),
        "version_tag": f"v{new_seq}",
        "version_seq": new_seq,
        "versioned_path": str(versioned_path),
        "bytes": len(content),
    }


async def list_file_versions(file_path: str) -> list[dict[str, Any]]:
    """
    列出文件的所有版本。

    返回：按 version_seq 降序排列
    """
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, version_tag, version_seq, commit_hash, task_id, created_at
            FROM file_versions
            WHERE file_path = $1
            ORDER BY version_seq DESC
            """,
            [file_path],
        )

    return [
        {
            "version_id": str(row["id"]),
            "version_tag": row["version_tag"],
            "version_seq": row["version_seq"],
            "commit_hash": row["commit_hash"],
            "task_id": str(row["task_id"]) if row["task_id"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


async def rollback_file(
    file_path: str,
    target_version_tag: str,
) -> dict[str, Any]:
    """
    将文件回滚到指定版本。

    流程：
    1. 查询目标版本是否存在
    2. 备份当前文件为新版本（copy-on-write）
    3. 用目标版本覆盖原文件
    4. 记录回滚操作
    """
    if not _is_versionable_path(file_path):
        raise FileVersioningError(f"Path {file_path!r} is not versionable")

    async with acquire() as conn:
        # 查询目标版本
        target_row = await conn.fetchrow(
            """
            SELECT id, versioned_path, version_tag, version_seq
            FROM file_versions
            WHERE file_path = $1 AND version_tag = $2
            """,
            [file_path, target_version_tag],
        )
        if not target_row:
            raise FileVersioningError(
                f"Version {target_version_tag!r} not found for {file_path!r}"
            )

        # 备份当前版本
        current_p = Path(file_path)
        if current_p.exists():
            backup_seq = (await conn.fetchrow(
                "SELECT COALESCE(MAX(version_seq), 0) + 1 AS next_seq "
                "FROM file_versions WHERE file_path = $1",
                [file_path],
            ))["next_seq"]
            backup_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            backup_path = _resolve_version_path(file_path, backup_seq, backup_timestamp)
            shutil.copy2(current_p, backup_path)

            backup_id = uuid.uuid4()
            await conn.execute(
                """
                INSERT INTO file_versions
                    (id, file_path, versioned_path, version_tag, version_seq, task_id)
                VALUES ($1, $2, $3, $4, $5, NULL)
                """,
                [backup_id, file_path, str(backup_path),
                 f"v{backup_seq}", backup_seq],
            )

        # 覆盖原文件
        target_content = Path(target_row["versioned_path"]).read_text()
        current_p.write_text(target_content, encoding="utf-8")

        await conn.commit()

    logger.info(
        "file_rollback",
        file_path=file_path,
        from_version=target_version_tag,
        new_backup=f"v{backup_seq}" if current_p.exists() else "no_backup",
    )

    return {
        "rolled_back": file_path,
        "to_version": target_version_tag,
        "backup_version": f"v{backup_seq}" if current_p.exists() else None,
    }


# ── Git 集成 ───────────────────────────────────────────────────

def git_commit(
    workspace: str,
    task_id: str,
    file_paths: list[str],
    message: str | None = None,
) -> str | None:
    """
    将变更文件提交到 Git 仓库。

    返回：commit hash 或 None（提交失败）
    """
    repo = Path(workspace)
    if not (repo / ".git").exists():
        return None

    commit_msg = message or f"[Task {task_id}] Auto-generated by Agent"

    try:
        for fp in file_paths:
            subprocess.run(
                ["git", "-C", str(repo), "add", fp],
                check=True,
                capture_output=True,
            )
        result = subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", commit_msg],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("git_commit_failed", stderr=result.stderr)
            return None

        hash_result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        commit_hash = hash_result.stdout.strip()
        logger.info("git_commit_success", commit=commit_hash, task_id=task_id)
        return commit_hash

    except subprocess.CalledProcessError as exc:
        logger.warning("git_operation_failed", error=str(exc))
        return None
