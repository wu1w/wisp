from __future__ import annotations

"""
Docker 沙箱管理器。

所有沙箱操作必须通过 SandboxService，严禁直接调用 subprocess 或 docker SDK。
资源限制：CPU 1核 / 内存 512MB / 网络隔离 / 10MB tmpfs / 最长 5 分钟
"""

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from src.utils.config import get_config

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = structlog.get_logger(__name__)

# Docker 客户端单例（惰性初始化）
_docker_client: Any = None


class SandboxUnavailableError(Exception):
    """Docker 沙箱不可用时抛出（如 Docker Daemon 未运行）。"""
    pass


def _get_docker() -> Any:
    """
    获取 Docker 客户端（惰性初始化）。

    首次调用时尝试连接 Docker Daemon，失败时抛出 SandboxUnavailable。
    """
    global _docker_client
    if _docker_client is None:
        try:
            from docker import DockerClient  # type: ignore[attr-defined]
            _docker_client = DockerClient.from_env()
            # 验证连接：ping Docker Daemon
            _docker_client.ping()
            logger.debug("docker_client_connected")
        except Exception as exc:
            _docker_client = None
            raise SandboxUnavailableError(f"Docker not available: {exc}") from exc
    return _docker_client


class SandboxService:
    """
    Docker 沙箱执行服务。

    职责：
    - 启动沙箱容器（CPU 1核 / 内存 512MB / 网络隔离 / 10MB tmpfs）
    - execute()：在沙箱内执行命令
    - 沙箱退出后自动将 /workspace/output 归档到对象存储
    """

    def __init__(self) -> None:
        self._config = get_config()
        self._sandbox_cfg = self._config["sandbox"]

    def is_available(self) -> bool:
        """检查 Docker 是否可用。"""
        try:
            _get_docker()
            return True
        except SandboxUnavailableError:
            return False

    async def execute(
        self,
        command: str,
        lang: str = "bash",
        timeout: int = 60,
        workdir: str = "/workspace",
    ) -> dict[str, Any]:
        """
        在沙箱容器内执行命令。

        参数：
            command: 待执行的命令或代码
            lang: bash | python3 | node | javascript
            timeout: 超时秒数（最大 300s）
            workdir: 工作目录（默认 /workspace）

        返回：
            {
                "stdout": str,
                "stderr": str,
                "exit_code": int,
                "duration_ms": int,
                "killed": bool,
            }

        异常：
            SandboxUnavailable: Docker 不可用时（已由调用方捕获并降级本地执行）
        """
        timeout = min(timeout, self._sandbox_cfg["max_timeout_seconds"])
        sandbox_id = uuid.uuid4().hex[:8]
        container_name = f"wisp-sandbox-{sandbox_id}"

        cfg = self._sandbox_cfg
        image = cfg["image"]

        logger.info(
            "sandbox_execute_start",
            sandbox_id=sandbox_id,
            lang=lang,
            timeout=timeout,
        )

        # 获取 docker client（可能抛出 SandboxUnavailable）
        docker = _get_docker()

        try:
            # 拉取镜像（如果本地不存在）
            await self._ensure_image(docker, image)

            # 启动容器
            container: Container = docker.containers.run(
                image=image,
                name=container_name,
                command="sleep infinity",  # 保持容器运行
                cpu_period=100000,
                cpu_quota=int(cfg["cpu"] * 100000),
                mem_limit=cfg["memory_mb"],
                memswap_limit=cfg["memory_mb"],
                network_mode="none",
                tmpfs={"/tmp": f"size={cfg['tmpfs_mb']}m"},
                detach=True,
                auto_remove=False,
            )

            try:
                # 等容器完全启动
                await asyncio.sleep(0.5)
                container.reload()

                # 执行命令
                exec_result = await self._do_exec(
                    container,
                    command,
                    lang,
                    workdir,
                    timeout,
                )
                return exec_result

            finally:
                # 清理容器
                try:
                    container.stop(timeout=2)
                    container.remove()
                    logger.debug("sandbox_container_cleaned", container=container_name)
                except Exception as exc:
                    logger.warning("sandbox_cleanup_failed", container=container_name, error=str(exc))

        except SandboxUnavailableError:
            raise  # 向上传递，由 tools.py 捕获并降级
        except TimeoutError:
            logger.error("sandbox_timeout", sandbox_id=sandbox_id, timeout=timeout)
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout}s",
                "exit_code": -1,
                "duration_ms": timeout * 1000,
                "killed": True,
            }
        except Exception as exc:
            logger.exception("sandbox_execute_error", sandbox_id=sandbox_id, error=str(exc))
            return {
                "stdout": "",
                "stderr": f"Sandbox error: {exc}",
                "exit_code": -1,
                "duration_ms": 0,
                "killed": False,
            }

    async def _ensure_image(self, docker: Any, image: str) -> None:
        """确保镜像存在，不存在则拉取（在线程池执行）。"""
        loop = asyncio.get_event_loop()

        def _pull():
            try:
                docker.images.get(image)
            except Exception:
                logger.info("sandbox_pulling_image", image=image)
                docker.images.pull(image)

        await loop.run_in_executor(None, _pull)

    async def _do_exec(
        self,
        container: Container,
        command: str,
        lang: str,
        workdir: str,
        timeout: int,
    ) -> dict[str, Any]:
        """在容器内执行命令，等待完成并返回结果。"""
        import time

        # 构造实际执行命令
        if lang == "python3":
            exec_cmd = f"python3 -c {command!r}"
        elif lang in ("node", "javascript"):
            exec_cmd = f"node -e {command!r}"
        else:  # bash
            exec_cmd = command

        start_ms = int(time.time() * 1000)

        # 使用 exec_create + exec_start（同步版在线程池）
        loop = asyncio.get_event_loop()
        exit_code, stdout, stderr = await loop.run_in_executor(
            None,
            lambda: self._sync_exec(container, exec_cmd, workdir, timeout),
        )

        duration_ms = int(time.time() * 1000) - start_ms
        killed = (exit_code == -1 and duration_ms >= timeout * 1000)

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "killed": killed,
        }

    def _sync_exec(
        self,
        container: Container,
        command: str,
        workdir: str,
        timeout: int,
    ) -> tuple[int, str, str]:
        """同步执行命令（在线程池调用）。"""

        # docker exec 的伪终端方式
        exec_id = container.exec_create(
            cmd=["/bin/sh", "-c", command],
            workdir=workdir,
            stdout=True,
            stderr=True,
            tty=True,
        )

        # 设置 socket 超时
        sock = container.client.api._get_raw_response_socket(exec_id)
        sock.settimeout(timeout)

        try:
            output = sock.recv(4096).decode("utf-8", errors="replace")
        except TimeoutError:
            sock.close()
            return -1, "", f"Command timed out after {timeout}s"

        exit_code = container.exec_start(exec_id, demux=True)
        # exec_start 返回 (stdout, stderr)，均为 bytes | None
        if isinstance(output, tuple):
            stdout_bytes, stderr_bytes = output
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        else:
            stdout = output
            stderr = ""

        return exit_code or 0, stdout, stderr

    async def archive_output(self, task_id: str, container_id: str | None = None) -> dict[str, str]:
        """
        将沙箱 /workspace/output 归档到对象存储。

        由 Worker 在沙箱退出后调用。
        """
        # TODO: 实现产出物归档到 MinIO
        logger.info("sandbox_archive", task_id=task_id)
        return {}


# 全局单例（docker 客户端在首次使用时才初始化）
sandbox_service = SandboxService()
