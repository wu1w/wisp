"""基础单元测试（可本地运行，无需 DB/MinIO/模型）。"""

import os
import sys

import pytest

# ── Config Tests ────────────────────────────────────────────────

class TestConfig:
    def test_resolve_env_var(self):
        """测试 ${ENV_VAR} 占位符替换。"""
        os.environ["TEST_VAR"] = "hello"
        # 清除缓存模块
        for key in list(sys.modules.keys()):
            if key.startswith("src."):
                del sys.modules[key]

        from src.utils.config import _resolve_env
        result = _resolve_env({"key": "${TEST_VAR}"})
        assert result["key"] == "hello"

    def test_deep_merge(self):
        """测试字典深度合并。"""
        for key in list(sys.modules.keys()):
            if key.startswith("src."):
                del sys.modules[key]

        from src.utils.config import _deep_merge

        base = {"a": 1, "b": {"c": 2}}
        override = {"b": {"d": 3}, "e": 4}
        result = _deep_merge(base, override)

        assert result["a"] == 1
        assert result["b"]["c"] == 2
        assert result["b"]["d"] == 3
        assert result["e"] == 4


# ── Security Tests ──────────────────────────────────────────────

class TestSecurity:
    def test_validate_path_allowed_existing(self):
        """测试合法路径（allow_create=False 时存在性检查）。"""
        from pathlib import Path
        from unittest.mock import patch

        from src.utils.security import ALLOWED_PREFIXES, validate_path

        # 白名单内找一个实际存在的目录
        allowed_existing = None
        for prefix in ALLOWED_PREFIXES:
            dir_path = Path(prefix.rstrip("/"))
            if dir_path.exists():
                allowed_existing = dir_path
                break

        if allowed_existing:
            # 真实存在的白名单目录 → 验证 allow_create=False 通过
            p = validate_path(str(allowed_existing), allow_create=False)
            assert p.exists()
        else:
            # 无真实目录 → mock Path.exists 返回 True，验证前缀检查通过
            test_path = "/home/ubuntu/workspace/exists"
            with patch.object(Path, "exists", return_value=True):
                p = validate_path(test_path, allow_create=False)
            assert str(p).startswith("/home/ubuntu/workspace/")

    def test_validate_path_allowed_create(self):
        """测试合法路径（不存在，允许创建）。"""
        from src.utils.security import validate_path

        # 使用 workspace 子目录（白名单内）
        test_dir = "/home/ubuntu/workspace/wisp-test"
        test_file = os.path.join(test_dir, "new_file.txt")
        p = validate_path(test_file, allow_create=True)
        assert str(p).startswith("/home/ubuntu/workspace/")

    def test_validate_path_forbidden(self):
        """测试非法路径拦截。"""
        from src.utils.security import SecurityError, validate_path

        with pytest.raises(SecurityError):
            validate_path("/etc/passwd")
        with pytest.raises(SecurityError):
            validate_path("/root/.bashrc")

    def test_filter_sensitive_quoted(self):
        """测试带引号的敏感信息脱敏。"""
        from src.utils.security import filter_sensitive

        # 双引号格式
        r = filter_sensitive('api_key="sk-abc123"')
        assert "sk-abc123" not in r
        assert "***" in r

        # 单引号格式
        r = filter_sensitive("api_key='sk-xyz789'")
        assert "sk-xyz789" not in r

        # 冒号格式
        r = filter_sensitive("password: secretpass")
        assert "secretpass" not in r

    def test_filter_sensitive_unquoted(self):
        """测试无引号敏感信息脱敏。"""
        from src.utils.security import filter_sensitive

        r = filter_sensitive("api_key=sk-nopassword")
        assert "sk-nopassword" not in r
        assert "***" in r


# ── ETL Tests ──────────────────────────────────────────────────

class TestETL:
    def test_normalize_truncation(self):
        """测试内容截断。"""
        from src.services.etl import normalize

        long_content = "a" * 20000
        result = normalize(long_content, max_chars=10000)
        assert len(result) <= 10000 + 60

    def test_normalize_strip(self):
        """测试空白清理。"""
        from src.services.etl import normalize

        result = normalize("  hello world  \n\n")
        assert result == "hello world"

    def test_filter_sensitive_quoted_api_key(self):
        """测试 API Key（带引号）脱敏。"""
        from src.services.etl import filter_sensitive

        r = filter_sensitive('api_key="sk-abc123"')
        assert "sk-abc123" not in r
        assert "***" in r

    def test_filter_sensitive_quoted_password(self):
        """测试 Password（带引号）脱敏。"""
        from src.services.etl import filter_sensitive

        r = filter_sensitive("password='secret123'")
        assert "secret123" not in r

    def test_filter_sensitive_unquoted(self):
        """测试无引号 Password 脱敏。"""
        from src.services.etl import filter_sensitive

        r = filter_sensitive("password=mypassword")
        assert "mypassword" not in r

    def test_filter_sensitive_private_key(self):
        """测试私钥脱敏：证书头被替换为占位符。"""
        from src.services.etl import filter_sensitive

        # 使用标准的 RSA 私钥格式（中间有 "RSA" 单词，符合正则 -----BEGIN [A-Z]+ PRIVATE KEY-----）
        r = filter_sensitive("mypassword\n-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        # 证书头被替换为 [REDACTED PRIVATE KEY]，私钥内容不再以证书头开头
        assert "-----BEGIN RSA PRIVATE KEY-----" not in r
        assert "[REDACTED PRIVATE KEY]" in r

    def test_dedupe_content_deterministic(self):
        """测试 HMAC 去重哈希确定性。"""
        from src.services.etl import dedupe_content

        h1 = dedupe_content("hello world", "test-key")
        h2 = dedupe_content("hello world", "test-key")
        assert h1 == h2

    def test_dedupe_content_different_for_different_content(self):
        """测试不同内容产生不同哈希。"""
        from src.services.etl import dedupe_content

        h1 = dedupe_content("hello", "test-key")
        h2 = dedupe_content("world", "test-key")
        assert h1 != h2

    def test_etl_pipeline_noop(self):
        """ETL pipeline：clean 内容直接通过。"""
        from src.services.etl import dedupe_content, filter_sensitive, normalize

        content = "hello world"
        normalized = normalize(content)
        filtered = filter_sensitive(normalized)
        hash_ = dedupe_content(filtered, "test-key")
        assert hash_ is not None
        # dedupe_content 返回 HMAC-SHA256 前16字符
        assert len(hash_) == 16

    def test_etl_pipeline_sensitive_filtered(self):
        """ETL pipeline：含密码内容脱敏后哈希与干净内容不同。"""
        from src.services.etl import filter_sensitive, normalize

        clean = "hello world"
        with_password = "password=secret123"

        clean_norm = normalize(clean)
        dirty_norm = normalize(with_password)

        clean_filtered = filter_sensitive(clean_norm)
        dirty_filtered = filter_sensitive(dirty_norm)

        # 脱敏后内容不应该泄露密码（整个 key=value 被替换为 ***）
        assert "secret123" not in dirty_filtered
        assert dirty_filtered == "***"
        # clean 内容应该不变
        assert clean_filtered == clean

    def test_etl_pipeline_duplicate_detection(self):
        """ETL pipeline：完全相同内容产生相同哈希。"""
        from src.services.etl import dedupe_content, filter_sensitive, normalize

        text = "  hello world  "
        h1 = dedupe_content(filter_sensitive(normalize(text)), "key-a")
        h2 = dedupe_content(filter_sensitive(normalize(text)), "key-a")
        assert h1 == h2  # normalize + dedupe 幂等

    def test_etl_private_key_not_leaked(self):
        """私钥内容经过 ETL pipeline 不会以证书头形式泄露。"""
        from src.services.etl import filter_sensitive, normalize

        content = """
        My private key:
        -----BEGIN EC PRIVATE KEY-----
        MHQCAQEEILSHZfXr3Lp6cKGFbUWVnL2v3pCtnLV
        o衍生的私钥内容
        """
        normalized = normalize(content)
        filtered = filter_sensitive(normalized)
        # 证书头不应该出现在过滤结果中
        assert "-----BEGIN EC PRIVATE KEY-----" not in filtered
