#!/usr/bin/env python3
"""
Wisp 数据库备份脚本。

功能：
  - pg_dump 全量备份 + gzip 压缩
  - 保留 7 天备份（自动清理旧文件）
  - 备份完成后打印状态

用法（手动）：
  cd /home/ubuntu/wisp
  source .venv/bin/activate
  source .env
  python scripts/backup_db.py

用法（cron，每日 03:00）：
  0 3 * * * cd /home/ubuntu/wisp && .venv/bin/python scripts/backup_db.py >> logs/backup.log 2>&1
"""

import datetime
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── Bootstrap ──────────────────────────────────────────────────
# 确保 .env 中的变量对 os.environ 可见（load_dotenv 不会覆盖已有环境变量）
from dotenv import load_dotenv
_dotenv_path = Path(__file__).parent.parent / ".env"
load_dotenv(_dotenv_path, override=False)

# ── 日志配置 ──────────────────────────────────────────────────
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "backup.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── 配置 ───────────────────────────────────────────────────────
BACKUP_DIR = Path(os.environ.get("WISP_BACKUP_DIR", "/home/ubuntu/wisp/backups"))
RETENTION_DAYS = 7

DB_HOST = os.environ.get("DATABASE_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DATABASE_PORT", "5432")
DB_NAME = os.environ.get("DATABASE_NAME", "wisp")
DB_USER = os.environ.get("DATABASE_USER", "wisp")
DB_PASSWORD = os.environ.get("DATABASE_PASSWORD", "")


def get_pg_dump_path() -> Path:
    """查找 pg_dump 路径。"""
    for candidate in ["/usr/bin/pg_dump", "/usr/local/bin/pg_dump"]:
        p = Path(candidate)
        if p.exists():
            return p
    # 尝试 which
    result = shutil.which("pg_dump")
    if result:
        return Path(result)
    raise FileNotFoundError("pg_dump not found. Install postgresql-client.")


def run_backup(backup_path: Path) -> bool:
    """执行 pg_dump 备份。"""
    env = os.environ.copy()
    env["PGPASSWORD"] = DB_PASSWORD

    cmd = [
        str(get_pg_dump_path()),
        "-h", DB_HOST,
        "-p", DB_PORT,
        "-U", DB_USER,
        "-d", DB_NAME,
        "-Fc",           # custom format (compressed, supports restore options)
        "-f", str(backup_path),
    ]

    logger.info(f"[backup] starting pg_dump to {backup_path}, db={DB_HOST}:{DB_PORT}/{DB_NAME}")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info(f"[backup] success, path={backup_path}, size={backup_path.stat().st_size} bytes")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"[backup] pg_dump failed, returncode={e.returncode}, stderr={e.stderr}")
        # 备份失败，删除不完整的文件
        if backup_path.exists():
            backup_path.unlink()
        return False


def cleanup_old_backups(backup_dir: Path, retention_days: int) -> list[Path]:
    """删除超过保留天数的备份文件。"""
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    removed: list[Path] = []

    for f in backup_dir.glob("wisp_backup_*.dump.gz"):
        if f.stat().st_mtime < cutoff.timestamp():
            f.unlink()
            removed.append(f)
            logger.info(f"[cleanup] removed old backup: {f}")

    return removed


def compress_dump(dump_path: Path, compressed_path: Path) -> bool:
    """gzip 压缩 dump 文件。"""
    try:
        with open(dump_path, "rb") as f_in:
            with open(compressed_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out, length=1024 * 1024)
        # 压缩后删除原始文件
        dump_path.unlink()
        logger.info(f"[backup] compressed {dump_path} -> {compressed_path}")
        return True
    except Exception as e:
        logger.error(f"[backup] compression failed: {e}")
        return False


def main() -> int:
    logger.info(f"[backup] script start, retention_days={RETENTION_DAYS}")

    # 确保备份目录存在
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # 生成文件名
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dump_path = BACKUP_DIR / f"wisp_backup_{timestamp}.dump"
    compressed_path = BACKUP_DIR / f"wisp_backup_{timestamp}.dump.gz"

    # 执行备份
    if not run_backup(dump_path):
        return 1

    # 压缩
    if not compress_dump(dump_path, compressed_path):
        return 1

    # 清理旧备份
    removed = cleanup_old_backups(BACKUP_DIR, RETENTION_DAYS)
    logger.info(f"[backup] complete, new_backup={compressed_path}, removed_count={len(removed)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
