#!/usr/bin/env python3
"""
数据库初始化脚本。

用法：
    python scripts/init_db.py --dry-run   # 仅打印 SQL
    python scripts/init_db.py             # 执行迁移
"""

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Wisp 数据库初始化")
    parser.add_argument("--dry-run", action="store_true", help="仅打印迁移 SQL，不执行")
    parser.add_argument("--revision", default="head", help="目标版本（默认: head）")
    args = parser.parse_args()

    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))

    cmd = [
        "alembic",
        "upgrade" if not args.dry_run else "upgrade",
        "--sql",  # always show SQL when dry-run
        args.revision,
    ]

    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=project_root)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
