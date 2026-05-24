"""配置加载：YAML + 环境变量，覆盖优先级 env > yaml > 默认值。"""

import os
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# 加载 .env 文件（显式指定路径，避免依赖 cwd）
_dotenv_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_dotenv_path)


def _resolve_env(value: Any) -> Any:
    """递归替换字符串值中的 ${ENV_VAR} 为环境变量。"""
    if isinstance(value, str):
        import re

        def replacer(m: re.Match[str]) -> str:
            var = m.group(1)
            return os.getenv(var, m.group(0))

        return re.sub(r"\$\{([^}]+)\}", replacer, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典。"""
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and k in result and isinstance(result[k], dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class Settings(BaseSettings):
    """应用全局配置（从环境变量读取的顶级字段）。"""

    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    minio_access_key: str | None = Field(default=None, validation_alias="MINIO_ACCESS_KEY")
    minio_secret_key: str | None = Field(default=None, validation_alias="MINIO_SECRET_KEY")


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    加载配置。

    优先级：环境变量 > config/default.yaml > 内置默认值
    字符串中的 ${ENV_VAR} 占位符自动替换为环境变量值。
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "default.yaml"
    else:
        config_path = Path(config_path)

    yaml_config: dict = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}

    # 环境变量注入（Settings 类处理）
    env_settings = Settings().model_dump(exclude_none=True)

    # 合并：yaml_config 为基础，env_settings 覆盖
    merged = _deep_merge(yaml_config, env_settings)

    # 递归解析 ${ENV_VAR}
    return cast(dict[str, Any], _resolve_env(merged))


# 全局配置单例（延迟加载）
_config: dict[str, Any] | None = None


def get_config() -> dict[str, Any]:
    """获取全局配置（单例）。"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
