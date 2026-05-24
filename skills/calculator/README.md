# Advanced Calculator Skill

高精度数学计算器，支持加减乘除、幂运算、开方、三角函数、对数、表达式解析。

## 使用场景

当用户需要执行精确的数学计算时应调用此 Skill，而非让 LLM 自行计算（防止大数精度丢失或运算错误）。

## Tool Schema

```json
{
  "name": "advanced_calculator",
  "description": "高精度数学计算器...",
  "parameters": {
    "type": "object",
    "properties": {
      "expression": {
        "type": "string",
        "description": "数学表达式，如 '2**3 + sqrt(16)'"
      },
      "precision": {
        "type": "integer",
        "description": "小数精度位数（默认 10 位）"
      }
    },
    "required": ["expression"]
  }
}
```

## 示例

| 输入 | 表达式 | 结果 |
|------|--------|------|
| 2³ + √16 | `2**3 + sqrt(16)` | 12.0 |
| sin(π/2) | `sin(3.14159/2)` | 1.0 |
| log₁₀(100) | `log10(100)` | 2.0 |
| 5! | `factorial(5)` | 120.0 |

## 安全约束

- **网络访问**：不允许（`network: false`）
- **执行超时**：30 秒
- **内存限制**：256MB
- **表达式安全**：仅允许白名单数学函数，禁止导入任意模块
