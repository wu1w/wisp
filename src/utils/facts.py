"""
extract_core_facts: 将长对话历史压缩为结构化 Fact。

触发条件：对话上下文长度超过 EXTERNALIZE_THRESHOLD_CHARS（默认 4000 字符）
时，自动将"明确的事实"提取为结构化字段，避免 LLM summarization 捏造细节。

红线规则（SPEC.md 六.7）：
- 禁止对近 5 轮对话原文做 summarization
- 禁止对当前执行中的代码文件做压缩
- 禁止对报错栈（traceback）做摘要
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.models.schemas import LLMMessage

# ── Prompt Template ────────────────────────────────────────────

_EXTRACT_FACTS_PROMPT = """\
You are a context compression engine. Extract structured facts from the conversation history below.

IMPORTANT RULES:
- Do NOT invent facts not present in the conversation
- Do NOT summarize code, error traces, or configuration files
- Only extract: goals, decisions,已完成 steps, known errors, file changes
- If information is missing for a field, use null (not a guess)

Conversation History:
{history}

Output a JSON object with this exact structure:
{{
  "task_goal": "The overall task objective (or null if unclear)",
  "current_step": "What the agent was doing when context was captured",
  "completed_steps": [
    {{"seq": 1, "action": "brief description", "outcome": "success|failure|running"}}
  ],
  "known_errors": [
    {{"error_type": "PermissionError|SyntaxError|NotFoundError|...", "reason": "brief", "fix_status": "pending|fixed"}}
  ],
  "file_changes": {{
    "/path/to/file": {{"purpose": "why this file was changed", "key_lines": "key lines or null"}}
  }},
  "next_action": "What the agent plans to do next (or null)"
}}
"""


# ── Thresholds ────────────────────────────────────────────────

EXTERNALIZE_THRESHOLD_CHARS = 4000


def should_externalize(messages: list[dict[str, Any]]) -> bool:
    """判断是否需要 externalize（超过字符阈值）。"""
    total = sum(
        len(m.get("content", ""))
        for m in messages
        if m.get("role") != "system"
    )
    return total > EXTERNALIZE_THRESHOLD_CHARS


def extract_core_facts(
    messages: list[dict[str, Any]],
    llm_gateway: Any = None,
) -> dict[str, Any]:
    """
    提取结构化 Facts。

    如果提供了 llm_gateway，则调用 LLM 生成结构化 facts。
    否则基于规则从消息中提取（fallback 模式）。

    参数：
        messages: 对话历史（不含 system prompt）
        llm_gateway: 可选，LLMGateway 实例用于 LLM 提取

    返回：
        结构化 facts dict
    """
    # Filter out system messages and tool results for the prompt
    conversation_only = [
        m for m in messages
        if m.get("role") in ("user", "assistant")
    ]

    history_text = "\n".join(
        f"[{m.get('role')}] {m.get('content', '')[:500]}"
        for m in conversation_only[-10:]  # 最多最近 10 轮
    )

    if llm_gateway is not None:
        return _extract_via_llm(llm_gateway, history_text)

    return _extract_via_rules(conversation_only)


def _extract_via_llm(gateway: Any, history_text: str) -> dict[str, Any]:
    """通过 LLM 提取结构化 facts（精确但需消耗 token）。"""
    try:
        response = gateway.chat(
            messages=[
                LLMMessage(
                    role="user",
                    content=_EXTRACT_FACTS_PROMPT.format(history=history_text),
                )
            ],
            profile="cheap",
            tools=None,
            temperature=0.1,
            max_tokens=800,
        )
        content = response.content or "{}"
        # 提取 JSON
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
        else:
            parsed = json.loads(content)
        return _sanitize_facts(parsed)
    except Exception:
        return _empty_facts()


def _extract_via_rules(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    基于规则的 fallback 提取（无 LLM 时使用）。

    从消息中提取：tool_calls、error 关键字、file 路径变更。
    """
    facts = _empty_facts()
    completed: list[dict[str, Any]] = []
    tool_results: list[str] = []

    for i, m in enumerate(messages):
        role = m.get("role", "")
        content = m.get("content", "")

        if role == "assistant":
            # 提取 tool_call 意图
            if "tool_calls" in str(content) or "search_memory" in str(content):
                facts["current_step"] = content[:100]

        elif role == "tool":
            tool_results.append(content[:200])
            # 检测错误
            if anykw(content, ["error", "failed", "exception", "traceback"]):
                facts["known_errors"].append({
                    "error_type": _classify_error(content),
                    "reason": content[:100],
                    "fix_status": "pending",
                })

        elif role == "user":
            if i == 0:
                facts["task_goal"] = content[:200]

    # 从 tool_results 推断 completed_steps
    for result in tool_results[-5:]:
        if anykw(result, ["success", "completed", "done"]):
            completed.append({
                "seq": len(completed) + 1,
                "action": result[:80],
                "outcome": "success",
            })

    facts["completed_steps"] = completed
    return facts


def _sanitize_facts(parsed: dict[str, Any]) -> dict[str, Any]:
    """确保 facts dict 字段完整，无缺失键。"""
    empty = _empty_facts()
    for key, val in empty.items():
        if key not in parsed:
            parsed[key] = val
    return parsed


def _empty_facts() -> dict[str, Any]:
    return {
        "task_goal": None,
        "current_step": None,
        "completed_steps": [],
        "known_errors": [],
        "file_changes": {},
        "next_action": None,
    }


def anykw(text: str, keywords: list[str]) -> bool:
    return any(kw.lower() in text.lower() for kw in keywords)


def _classify_error(content: str) -> str:
    """根据错误内容分类。"""
    content_lower = content.lower()
    if "permission" in content_lower or "denied" in content_lower:
        return "PermissionError"
    if "not found" in content_lower or "enoent" in content_lower:
        return "NotFoundError"
    if "syntaxerror" in content_lower or "syntax error" in content_lower:
        return "SyntaxError"
    if "timeout" in content_lower or "timed out" in content_lower:
        return "TimeoutError"
    if "connection" in content_lower or "network" in content_lower:
        return "NetworkError"
    return "Unknown"
