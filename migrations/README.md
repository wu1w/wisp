# Alembic 迁移说明

## 快速开始

```bash
cd /home/ubuntu/wisp

# 首次初始化（或查看将要执行的 SQL）
alembic upgrade head

# 仅打印 SQL，不执行
alembic upgrade head --sql

# 查看当前版本
alembic current

# 查看迁移历史
alembic history

# 回滚一个版本
alembic downgrade -1

# 自动检测模型变更，生成新迁移（模型改后才用）
alembic revision --autogenerate -m "add new_column"
```

## 环境变量

数据库连接从 `config/default.yaml` 读取，alembic.ini 中的 `sqlalchemy.url` 会被 `migrations/env.py` 覆盖。

```bash
export DATABASE_URL="postgresql+asyncpg://wisp:***@localhost:5432/wisp"
```

## 重要约束

- 所有表通过 `src/models/tables.py` 中的 SQLAlchemy 模型定义
- 新增或修改表时：先改 `tables.py`，再运行 `alembic revision --autogenerate`
- 手动编辑迁移文件仅限紧急修复，禁止在生产环境手动修改
- 迁移脚本中禁止使用 `DROP COLUMN CASCADE`，用 `DROP COLUMN` 配合单独迁移

## 版本说明

- `001_initial`: 初始版本，创建全部 16 张表
- 后续迁移严格按依赖顺序（先建表先删表）
