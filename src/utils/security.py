"""安全工具：路径白名单、敏感信息过滤。"""

import re
from pathlib import Path

# 路径白名单：只允许访问以下目录
ALLOWED_PREFIXES: tuple[str, ...] = (
    "/home/ubuntu/workspace/",
    "/tmp/wisp/",
    "/workspace/",
    "/tmp/",   # 集成测试用临时目录（需 trailing / 防止 /tmpfile 误匹配）
    # 注意：/tmp 目录自身由下方的精确匹配逻辑处理
)

# 系统敏感路径（绝对禁止访问）
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/etc/"),
    re.compile(r"^/usr/"),
    re.compile(r"^/root/"),
    re.compile(r"^/var/log/"),
    re.compile(r"^/proc/"),
    re.compile(r"^/\.git/"),
    re.compile(r"^/\.ssh/"),
)

# 敏感信息正则
# 使用原始字符串 r"..." 避免转义地狱
SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key="value" 或 key='value' 格式（引号成对替换）
    (
        re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*(["\'])([^\"\']{1,50})\2'),
        "***",
    ),
    (
        re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*(["\'])([^\"\']{1,50})\2'),
        "***",
    ),
    (
        re.compile(r'(?i)(token|auth[_-]?token|access[_-]?token)\s*[=:]\s*(["\'])([^\"\']{1,50})\2'),
        "***",
    ),
    # 无引号 key=value 格式
    (
        re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*([^\s]{1,50})'),
        "***",
    ),
    (
        re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*([^\s]{1,50})'),
        "***",
    ),
    (
        re.compile(r'(?i)(token|auth[_-]?token|access[_-]?token)\s*[=:]\s*([^\s]{1,50})'),
        "***",
    ),
    # 证书
    (
        re.compile(r"-----BEGIN [A-Z]+ PRIVATE KEY-----"),
        "[REDACTED PRIVATE KEY]",
    ),
    (
        re.compile(r"-----BEGIN [A-Z]+ PUBLIC KEY-----"),
        "[REDACTED PUBLIC KEY]",
    ),
]


class SecurityError(Exception):
    """安全检查失败时抛出。"""

    pass


def validate_path(path: str, allow_create: bool = False) -> Path:
    """
    验证文件路径是否安全。

    规则：
    1. 路径必须在 ALLOWED_PREFIXES 之一内
    2. 路径不得匹配 FORBIDDEN_PATTERNS
    3. 若 allow_create=False，路径必须已存在

    返回标准化后的 Path 对象。

    抛出：
        SecurityError：路径不安全或不存在（且不允许创建）
    """
    p = Path(path).expanduser().resolve()

    # 前缀检查
    path_str = str(p)
    allowed = False
    for prefix in ALLOWED_PREFIXES:
        if prefix.endswith("/"):
            # 带 / 的前缀要求严格前缀匹配
            if path_str.startswith(prefix):
                allowed = True
                break
        else:
            # 不带 / 的前缀（如 /tmp）只允许精确匹配或 /prefix/... 子路径
            if path_str == prefix or path_str.startswith(prefix + "/"):
                allowed = True
                break
    # 额外：/tmp 目录自身（无 trailing slash）
    if not allowed and (path_str == "/tmp" or path_str.startswith("/tmp/")):
        allowed = True
    if not allowed:
        raise SecurityError(
            f"Path {path!r} is not under allowed prefixes: {ALLOWED_PREFIXES!r}"
        )

    # 敏感路径检查
    for pattern in FORBIDDEN_PATTERNS:
        if pattern.match(str(p)):
            raise SecurityError(f"Path {path!r} matches forbidden pattern: {pattern.pattern!r}")

    # 存在性检查
    if not allow_create and not p.exists():
        raise SecurityError(f"Path {path!r} does not exist (allow_create=False)")

    return p


def filter_sensitive(content: str) -> str:
    """
    对文本内容进行敏感信息脱敏。

    返回脱敏后的内容（原字符串不被修改）。
    """
    result = content
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
