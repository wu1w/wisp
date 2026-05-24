"""
JWT 认证中间件。

支持：
- Bearer Token 验证（Authorization: Bearer <token>）
- HS256 / RS256 算法
- 用户信息注入到 request.state
- 路径白名单（/health, /docs 等无需认证）

.env 配置：
    JWT_SECRET=your-secret-key          # HS256 对称密钥
    JWT_ALGORITHM=HS256               # HS256 或 RS256
    JWT_PUBLIC_KEY=---BEGIN PUBLIC KEY---...  # RS256 非对称公钥（可选）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import cast

import structlog
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

logger = structlog.get_logger(__name__)

# ── 配置 ────────────────────────────────────────────────────────

_JWT_SECRET = os.getenv("JWT_SECRET", "")
_JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
_JWT_PUBLIC_KEY = os.getenv("JWT_PUBLIC_KEY", "") or None

# 路径白名单（不需要认证）
_WHITELIST = {
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/v1/tasks",          # 任务创建接口开放（但 user_id 必填）
    "/v1/tasks/submit",   # 提交接口开放
}


# ── Data Class ─────────────────────────────────────────────────

@dataclass
class AuthenticatedUser:
    """认证用户信息。"""

    user_id: str
    username: str
    roles: list[str]
    exp: int | None = None


# ── Bearer Token 解析 ──────────────────────────────────────────

security = HTTPBearer(auto_error=False)


async def verify_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> AuthenticatedUser | None:
    """
    验证 JWT Token 并返回用户信息。

    Token 无效或过期返回 None（不抛异常，由调用方决定如何处理）。
    """
    if credentials is None:
        return None

    token = credentials.credentials

    try:
        # 解码选项
        options = {
            "verify_exp": True,
            "verify_iat": True,
            "verify_aud": False,
        }

        # 选择密钥
        if _JWT_ALGORITHM.startswith("RS"):
            if not _JWT_PUBLIC_KEY:
                logger.warning("jwt_rs256_but_no_public_key")
                return None
            key = _JWT_PUBLIC_KEY
        else:
            if not _JWT_SECRET:
                logger.warning("jwt_hs256_but_no_secret")
                return None
            key = _JWT_SECRET

        payload = jwt.decode(
            token,
            key,
            algorithms=[_JWT_ALGORITHM],
            options=options,
        )

        user_id = payload.get("sub") or payload.get("user_id")
        username = payload.get("username") or payload.get("email") or user_id or "unknown"
        roles = payload.get("roles", [])
        exp = payload.get("exp")

        if not user_id:
            return None

        return AuthenticatedUser(
            user_id=str(user_id),
            username=str(username),
            roles=roles if isinstance(roles, list) else [roles],
            exp=int(exp) if exp else None,
        )

    except jwt.ExpiredSignatureError:
        logger.debug("jwt_token_expired")
        return None
    except JWTError as exc:
        logger.warning("jwt_decode_failed", error=str(exc))
        return None


# ── FastAPI 依赖 ─────────────────────────────────────────────

from datetime import UTC

from fastapi import Depends


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser:
    """
    FastAPI 依赖：获取当前认证用户。

    要求 Authorization Header 携带有效 Bearer Token。
    无 token 或 token 无效 → 401 Unauthorized。
    """
    # 白名单路径直接放行
    if request.url.path in _WHITELIST or request.url.path.startswith("/docs"):
        # 白名单内的接口不强制认证（但有 token 则验证）
        user = await verify_token(credentials)
        if user is not None:
            return user
        # 白名单但无有效 token → 匿名用户（调用方决定如何处理）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 需要认证的路径
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await verify_token(credentials)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser | None:
    """
    FastAPI 依赖：获取当前用户（如有 token）。

    不强制要求认证，无 token 返回 None。
    """
    return await verify_token(credentials)


# ── Token 生成（供测试/登录接口使用）───────────────────────────

def create_access_token(
    user_id: str,
    username: str,
    roles: list[str] | None = None,
    expires_delta: int = 3600,
) -> str:
    """
    生成 JWT Access Token（供登录接口调用）。

    参数：
        user_id: 用户 ID
        username: 用户名
        roles: 角色列表
        expires_delta: 有效期（秒），默认 1 小时
    """
    if not _JWT_SECRET and _JWT_ALGORITHM != "RS256":
        raise RuntimeError(
            "JWT_SECRET not configured. Set JWT_SECRET environment variable."
        )

    from datetime import datetime

    payload = {
        "sub": user_id,
        "username": username,
        "roles": roles or [],
        "iat": int(datetime.now(UTC).timestamp()),
        "exp": int(datetime.now(UTC).timestamp()) + expires_delta,
    }

    key = _JWT_PUBLIC_KEY if _JWT_ALGORITHM.startswith("RS") else _JWT_SECRET
    token: str = cast(str, jwt.encode(payload, key, algorithm=_JWT_ALGORITHM))
    return token
