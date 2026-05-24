"""
Dreaming Validator — 熵减机制与幻觉检测。

职责：
- 检查输出 token 压缩比（必须 > 10）
- 检测典型幻觉词汇
- 验证 JSON 格式有效性
- 验证规则质量（不过于通用、长度合理）
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# ── 幻觉词汇黑名单 ───────────────────────────────────────────────

_HALLUCINATION_PATTERNS = [
    re.compile(r"As an AI", re.IGNORECASE),
    re.compile(r"\bI believe\b", re.IGNORECASE),
    re.compile(r"\bI think\b", re.IGNORECASE),
    re.compile(r"\bperhaps\b", re.IGNORECASE),
    re.compile(r"\bmight be\b", re.IGNORECASE),
    re.compile(r"\bmay be\b", re.IGNORECASE),
    re.compile(r"\btry to\b", re.IGNORECASE),
    re.compile(r"\bshould probably\b", re.IGNORECASE),
    re.compile(r"\bit is important to\b", re.IGNORECASE),
    re.compile(r"\bbe careful\b", re.IGNORECASE),
    re.compile(r"\bmake sure to\b", re.IGNORECASE),
    re.compile(r"\bnote that\b", re.IGNORECASE),
    re.compile(r"\bplease note\b", re.IGNORECASE),
    re.compile(r"^Note:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^Disclaimer:", re.IGNORECASE | re.MULTILINE),
]

# ── 过于通用的规则黑名单 ─────────────────────────────────────────

_GENERIC_PATTERNS = [
    re.compile(r"^always be careful", re.IGNORECASE),
    re.compile(r"^always check", re.IGNORECASE),
    re.compile(r"^make sure the", re.IGNORECASE),
    re.compile(r"^ensure that", re.IGNORECASE),
    re.compile(r"^remember to", re.IGNORECASE),
    re.compile(r"^always use", re.IGNORECASE),
    re.compile(r"^never forget", re.IGNORECASE),
]


class ValidationResult:
    """验证结果。"""

    def __init__(
        self,
        valid: bool,
        reasons: list[str],
        rules: list[dict[str, Any]] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.valid = valid
        self.reasons = reasons
        self.rules = rules or []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.compression_ratio = (
            float(input_tokens) / output_tokens if output_tokens > 0 else 0.0
        )


class DreamValidator:
    """
    Dreaming 产出验证器。

    验证维度：
    1. JSON 格式有效性
    2. 压缩比（input_tokens / output_tokens > 10）
    3. 无幻觉词汇
    4. 规则质量（不过于通用、长度合理）
    """

    # 硬性阈值
    MIN_COMPRESSION_RATIO = 10.0
    MAX_RULE_LENGTH = 200  # 字符
    MIN_RULE_LENGTH = 10   # 字符
    MIN_CONFIDENCE = 0.5

    def validate(
        self,
        output_content: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ValidationResult:
        """
        验证 LLM 产出是否满足安全标准。

        参数：
            output_content: LLM 返回的原始文本
            input_tokens: 输入 token 数（用于压缩比计算）
            output_tokens: 输出 token 数
        """
        reasons: list[str] = []

        # ── 1. JSON 格式验证 ──────────────────────────────────
        rules = self._parse_json(output_content)
        if rules is None:
            reasons.append("Invalid JSON output")
            return ValidationResult(
                valid=False,
                reasons=reasons,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        # ── 2. 压缩比检查（熵减核心）─────────────────────────
        if output_tokens > 0:
            ratio = input_tokens / output_tokens
            if ratio < self.MIN_COMPRESSION_RATIO:
                reasons.append(
                    f"Compression ratio {ratio:.1f} < {self.MIN_COMPRESSION_RATIO} "
                    f"(input={input_tokens}, output={output_tokens}). "
                    "Output is too long — likely hallucinating."
                )

        # ── 3. 逐条规则质量检查 ──────────────────────────────
        filtered_rules: list[dict[str, Any]] = []
        for rule_obj in rules:
            rule_text = rule_obj.get("rule", "")
            category = rule_obj.get("category", "general")
            confidence = float(rule_obj.get("confidence", 0.0))

            rule_reasons = self._validate_rule(rule_text, category, confidence)
            if rule_reasons:
                reasons.append(f"Rule dropped: {rule_reasons[0]}")
                continue

            filtered_rules.append({
                "rule": rule_text,
                "category": category,
                "confidence": confidence,
            })

        if not filtered_rules:
            reasons.append("All rules filtered out — no valid rules produced")

        valid = len(reasons) == 0 and len(filtered_rules) > 0
        if valid:
            logger.info(
                "dream_validation_passed",
                rules_count=len(filtered_rules),
                compression_ratio=input_tokens / output_tokens if output_tokens else 0,
            )
        else:
            logger.warning(
                "dream_validation_failed",
                reasons=reasons,
            )

        return ValidationResult(
            valid=valid,
            reasons=reasons,
            rules=filtered_rules,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _parse_json(self, content: str) -> list[dict[str, Any]] | None:
        """解析 JSON，容忍首尾空白和 markdown fences。"""
        # 去除 markdown code fence
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        if content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # 尝试提取 JSON 数组
            match = re.search(r"\[[\s\S]*\]", content)
            if match:
                try:
                    parsed = json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(parsed, list):
            return None
        return parsed

    def _validate_rule(
        self,
        rule_text: str,
        category: str,
        confidence: float,
    ) -> list[str]:
        """
        验证单条规则质量。

        返回空列表 = 有效；返回非空列表 = 无效原因。
        """
        reasons: list[str] = []

        # 长度检查
        if len(rule_text) < self.MIN_RULE_LENGTH:
            reasons.append(f"Rule too short ({len(rule_text)} chars): {rule_text[:30]}")
            return reasons
        if len(rule_text) > self.MAX_RULE_LENGTH:
            reasons.append(f"Rule too long ({len(rule_text)} chars): {rule_text[:30]}")

        # 置信度检查
        if confidence < self.MIN_CONFIDENCE:
            reasons.append(f"Confidence too low ({confidence}): {rule_text[:30]}")

        # 幻觉词汇检查
        for pattern in _HALLUCINATION_PATTERNS:
            if pattern.search(rule_text):
                reasons.append(f"Hallucination pattern detected: {rule_text[:30]}")
                return reasons  # 立即返回，不继续检查

        # 通用规则检查
        for pattern in _GENERIC_PATTERNS:
            if pattern.match(rule_text.strip()):
                reasons.append(f"Generic pattern detected: {rule_text[:30]}")
                return reasons

        # category 合法性
        valid_categories = {
            "network", "file_io", "shell", "git",
            "package_manager", "security", "api", "docker", "general",
        }
        if category not in valid_categories:
            reasons.append(f"Invalid category '{category}'")

        return reasons
