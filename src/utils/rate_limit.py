"""Rate Limiter 全局单例（避免循环导入）。"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    storage_uri="memory://",  # 单实例；生产换 redis://localhost:6379
)
