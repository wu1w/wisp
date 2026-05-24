"""
Alembic 迁移环境配置。

支持异步引擎（asyncpg），与 SQLAlchemy 2.0 风格保持一致。
autogenerate 会自动检测 src/models/tables.py 中的模型变更。
"""

from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 导入项目配置
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.tables import Base  # noqa: E402
from src.utils.config import get_config  # noqa: E402

# Alembic Config 对象
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 模型 MetaData（用于 autogenerate 自动检测变更）
target_metadata = Base.metadata

# 从项目配置注入数据库 URL（覆盖 alembic.ini 中的默认值）
_model_config = get_config()
db_cfg = _model_config["database"]
config.set_main_option(
    "sqlalchemy.url",
    f"postgresql+asyncpg://{db_cfg['user']}:{db_cfg['password']}"
    f"@{db_cfg['host']}:{db_cfg['port']}/{db_cfg['name']}",
)


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL 脚本，不需要数据库连接。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    异步在线模式：使用 async_engine 连接数据库执行迁移。

    与 run_migrations_online() 等效，但使用 asyncpg。
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def do_run_migrations(connection: Connection) -> None:
    """在连接上同步执行迁移（由 run_async_migrations 调用）。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """入口：在线模式运行迁移（异步兼容）。"""
    import asyncio
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
