"""
工具层集成测试（无需 DB/Redis/MinIO）。

测试 bash / read_file / write_file / list_dir 的实际行为。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.tools import (
    _bash_impl,
    _list_dir_impl,
    _read_file_impl,
    _write_file_impl,
)


class TestBashTool:
    """bash 工具测试。"""

    @pytest.mark.asyncio
    async def test_echo_ok(self):
        result = await _bash_impl("echo hello world")
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "hello world"

    @pytest.mark.asyncio
    async def test_stderr_captured(self):
        # stderr 被合并到 stdout（bash 2>&1 重定向）
        result = await _bash_impl("ls /nonexistent/path 2>&1")
        assert result["exit_code"] != 0
        assert "No such file" in result["stdout"]

    @pytest.mark.asyncio
    async def test_blocked_dangerous_command(self):
        result = await _bash_impl("rm -rf /")
        assert result["exit_code"] == 126
        assert "blocked" in result["stderr"].lower()

    @pytest.mark.asyncio
    async def test_blocked_shutdown(self):
        result = await _bash_impl("shutdown -h now")
        assert result["exit_code"] == 126

    @pytest.mark.asyncio
    async def test_timeout(self):
        result = await _bash_impl("sleep 10", timeout=1)
        # asyncio wait_for 超时后 proc.kill() → SIGKILL → exit_code=-9
        assert result["exit_code"] == -9
        assert result["timed_out"] is True

    @pytest.mark.asyncio
    async def test_workdir(self):
        # 创建临时目录作为 workdir
        with tempfile.TemporaryDirectory() as tmpdir:
            result = await _bash_impl(
                "pwd",
                workdir=tmpdir,
            )
            assert result["exit_code"] == 0
            assert tmpdir in result["stdout"]

    @pytest.mark.asyncio
    async def test_multiline_command(self):
        result = await _bash_impl("echo line1; echo line2")
        assert result["exit_code"] == 0
        assert "line1" in result["stdout"] and "line2" in result["stdout"]


class TestReadWriteFileTool:
    """read_file / write_file 工具测试。"""

    @pytest.mark.asyncio
    async def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = str(Path(tmpdir) / "roundtrip.txt")
            content = "hello wisp!\nsecond line\n中文测试"

            # 写入
            write_result = await _write_file_impl(path=file_path, content=content)
            assert write_result["error"] is None
            assert write_result["path"] == file_path
            assert write_result["bytes"] == len(content)

            # 读取
            read_result = await _read_file_impl(path=file_path)
            assert read_result["error"] is None
            assert read_result["content"] == content
            assert read_result["truncated"] is False

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = str(Path(tmpdir) / "a" / "b" / "c" / "nested.txt")
            result = await _write_file_impl(path=nested, content="deep")
            assert result["error"] is None
            assert Path(nested).exists()

    @pytest.mark.asyncio
    async def test_read_truncation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            long_file = Path(tmpdir) / "long.txt"
            long_content = "x" * 10_000
            long_file.write_text(long_content)

            result = await _read_file_impl(path=str(long_file), max_chars=100)
            assert result["truncated"] is True
            assert len(result["content"]) == 100 + len("\n... (truncated, total 10000 chars)")
            assert result["error"] is None

    @pytest.mark.asyncio
    async def test_read_nonexistent(self):
        result = await _read_file_impl("/nonexistent/file.txt")
        assert result["error"] is not None
        assert result["content"] is None

    @pytest.mark.asyncio
    async def test_write_outside_whitelist(self):
        result = await _write_file_impl(
            path="/etc/wisp_test.txt",
            content="should be blocked",
        )
        assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_append_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "append.txt")
            await _write_file_impl(path=path, content="line1\n")
            await _write_file_impl(path=path, content="line2\n", append=True)

            content = Path(path).read_text()
            assert content == "line1\nline2\n"


class TestListDirTool:
    """list_dir 工具测试。"""

    @pytest.mark.asyncio
    async def test_list_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建文件和小目录
            (Path(tmpdir) / "file.txt").write_text("hello")
            (Path(tmpdir) / "subdir").mkdir()
            (Path(tmpdir) / "subdir" / "nested.txt").write_text("nested")

            result = await _list_dir_impl(tmpdir)
            assert result["error"] is None
            names = [e["name"] for e in result["entries"]]
            assert "file.txt" in names
            assert "subdir" in names

            # 验证 type 字段
            file_entry = next(e for e in result["entries"] if e["name"] == "file.txt")
            assert file_entry["type"] == "file"
            dir_entry = next(e for e in result["entries"] if e["name"] == "subdir")
            assert dir_entry["type"] == "dir"

    @pytest.mark.asyncio
    async def test_list_nonexistent(self):
        result = await _list_dir_impl("/nonexistent/directory")
        assert result["error"] is not None
        assert result["entries"] is None
