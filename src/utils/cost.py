"""
LLM Token 成本计算（对应 agent-design.md 六.11.4）。

定价表（单位：USD / 1M tokens）。
国内模型（硅基流动等）可按需添加。

使用示例：
    from src.utils.cost import calc_llm_cost, calc_price_per_1m

    cost = calc_llm_cost(
        model="gpt-4o-mini",
        prompt_tokens=1000,
        completion_tokens=500,
    )
    print(f"本次调用成本: ${cost:.4f}")
"""

from __future__ import annotations

from typing import Any

# ── 定价表（USD / 1M tokens）─────────────────────────────────

LLM_TOKEN_PRICES: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    # Anthropic
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.25, "output": 1.25},
    "claude-3-opus": {"input": 15.00, "output": 75.00},
    "claude-3-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    # MiniMax (OpenAI compatible)
    "MiniMax-Text-01": {"input": 0.01, "output": 0.10},
    "abab6.5s-chat": {"input": 0.10, "output": 0.10},
    # DeepSeek
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-coder": {"input": 0.14, "output": 0.28},
    # Ollama (本地运行，无 API 费用)
    "llama3": {"input": 0.0, "output": 0.0},
    "llama3.1": {"input": 0.0, "output": 0.0},
    "qwen2.5": {"input": 0.0, "output": 0.0},
    "codellama": {"input": 0.0, "output": 0.0},
}


# ── 计算函数 ─────────────────────────────────────────────────

def calc_llm_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """
    计算一次 LLM 调用的成本（USD）。

    参数：
        model: 模型名（必须与 LLM_TOKEN_PRICES 中的 key 匹配）
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数

    返回：
        成本（USD），浮点数
    """
    prices = LLM_TOKEN_PRICES.get(model, {"input": 0.0, "output": 0.0})

    input_cost = (prompt_tokens / 1_000_000) * prices["input"]
    output_cost = (completion_tokens / 1_000_000) * prices["output"]

    return round(input_cost + output_cost, 6)


def calc_price_per_1m(model: str) -> dict[str, float]:
    """
    获取指定模型的每 1M token 价格。

    返回：
        {"input": float, "output": float} 或 {"input": 0.0, "output": 0.0}（未知模型）
    """
    return LLM_TOKEN_PRICES.get(model, {"input": 0.0, "output": 0.0})


def format_cost(cost_usd: float) -> str:
    """格式化成本显示。"""
    if cost_usd < 0.001:
        return f"${cost_usd * 1000:.2f}m"  # 毫美元
    return f"${cost_usd:.4f}"


def cost_from_response(response: Any) -> float | None:
    """
    从 LLM 响应对象提取 token 使用量并计算成本。

    支持：OpenAI SDK 格式 / dict 格式 / 直接 usage dict

    参数：
        response: LLM 响应对象或 usage dict

    返回：
        成本（USD）或 None（无法计算）
    """
    usage: dict[str, Any] | None = None

    # 尝试从对象属性提取
    if hasattr(response, "usage") and response.usage:
        usage = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
        }
    elif isinstance(response, dict):
        usage = response.get("usage")

    if not usage:
        return None

    # 从响应中提取 model（可能通过 resp.model 或显式传入）
    model = getattr(response, "model", None) or usage.get("model", "")

    # 清理 model 名称（如去除部署后缀）
    clean_model = _normalize_model_name(model)

    return calc_llm_cost(
        model=clean_model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )


def _normalize_model_name(model: str) -> str:
    """标准化模型名称，匹配定价表。"""
    if not model:
        return ""

    # 去掉常见部署后缀
    suffixes = ["/v1", "-2024", "-latest", "-beta"]
    clean = model
    for suffix in suffixes:
        clean = clean.removesuffix(suffix)

    # 精确匹配
    if clean in LLM_TOKEN_PRICES:
        return clean

    # 前缀匹配
    for known in LLM_TOKEN_PRICES:
        if clean.startswith(known) or known.startswith(clean):
            return known

    return model  # fallback：不匹配已知模型，按 0 计价
