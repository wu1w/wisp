#!/usr/bin/env python3
"""
Advanced Calculator — Skill 执行入口。

接收 JSON 参数，执行数学计算，返回 JSON 结果。

参数格式（通过 --params-json 传入）：
  {"expression": "2**3 + sqrt(16)", "precision": 10}

输出格式：
  {"result": {"value": 12.0, "expression": "2**3 + sqrt(16)", "precision": 10}, "exit_code": 0}
  {"error": "Division by zero", "exit_code": 1}
"""

import argparse
import json
import math
import sys
import re


def evaluate(expression: str, precision: int = 10) -> tuple[float, str | None]:
    """
    执行数学表达式求值。

    返回 (result, error_message)。
    """
    # 白名单：只允许数字、运算符、括号、math 模块函数
    SAFE_NAMES = {
        "pi": math.pi,
        "e": math.e,
        "sqrt": math.sqrt,
        "pow": pow,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "asin": math.asin,
        "acos": math.acos,
        "atan": math.atan,
        "log": math.log,
        "log10": math.log10,
        "log2": math.log2,
        "exp": math.exp,
        "abs": abs,
        "floor": math.floor,
        "ceil": math.ceil,
        "round": round,
        "factorial": math.factorial,
        "gcd": math.gcd,
    }

    # 安全检查：表达式只能包含数字、运算符、括号、空格和已知的数学函数名
    allowed_chars = set("0123456789+-*/()., _abcdefghijklmnopqrstuvwxyz")
    for ch in expression:
        if ch.lower() not in allowed_chars:
            return 0.0, f"Illegal character in expression: {ch!r}"

    # 将中缀函数名替换为计算结果
    expr = expression.strip()

    try:
        # 使用 eval 但只允许调用白名单中的函数
        # 注意：这里用 __import__ 禁用，不允许导入任何模块
        result = eval(expr, {"__builtins__": {}}, SAFE_NAMES)  # noqa: S307
        return round(float(result), precision), None
    except ZeroDivisionError:
        return 0.0, "Division by zero"
    except ValueError as exc:
        return 0.0, f"Math domain error: {exc}"
    except SyntaxError as exc:
        return 0.0, f"Syntax error: {exc}"
    except NameError as exc:
        return 0.0, f"Unknown function or variable: {exc}"
    except TypeError as exc:
        return 0.0, f"Type error: {exc}"
    except Exception as exc:
        return 0.0, f"Evaluation error: {exc}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced Calculator Skill")
    parser.add_argument("--params-json", required=True, help="JSON 参数文件路径")
    args = parser.parse_args()

    # 读取参数
    try:
        with open(args.params_json, encoding="utf-8") as f:
            params = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": f"Failed to read params: {exc}"}), file=sys.stdout)
        sys.exit(1)

    expression = params.get("expression", "")
    precision = int(params.get("precision", 10))

    if not expression:
        print(json.dumps({"error": "expression is required"}), file=sys.stdout)
        sys.exit(1)

    result, error = evaluate(expression, precision)

    if error:
        print(json.dumps({"error": error, "exit_code": 1}), file=sys.stdout)
        sys.exit(1)

    print(json.dumps({
        "result": {
            "value": result,
            "expression": expression,
            "precision": precision,
        },
        "exit_code": 0,
    }), file=sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
