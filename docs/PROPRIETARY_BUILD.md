# 闭源核心算法编译指南

Wisp 的核心算法（ETL 流水线、Evolution Engine 分析逻辑）已编译为 Cython `.so` 二进制文件。

## 发布说明

**.so 文件已经随开源包分发**，大多数情况下可直接使用，无需重新编译。

如果出现以下错误，需要重新编译：
```
ModuleNotFoundError: No module named 'src.core.proprietary.etl'
```
或 Python 版本不匹配（如克隆后发现 `.so` 是 Python 3.14 编译，但你的环境是 3.11）。

## 编译步骤

```bash
# 1. 安装 Cython
pip install cython

# 2. 编译 .so（必须在 wisp 项目根目录执行）
python scripts/compile_proprietary.py build

# 3. 验证
python scripts/compile_proprietary.py verify
```

## 目录结构

```
src/core/proprietary/
├── __init__.py              ✅ 上传（公开接口）
├── etl.pyx                  🔒 本地（闭源算法）
├── etl.cpython-314-x86_64-linux-gnu.so  ✅ 上传（编译产物）
├── evolution.pyx             🔒 本地（闭源算法）
└── evolution.cpython-314-x86_64-linux-gnu.so  ✅ 上传（编译产物）
```

## CI/CD 多 Python 版本支持

CI 使用 Python 3.11 构建自己的 `.so` 文件并上传到 git：

```yaml
- name: Build proprietary .so modules
  run: |
    uv run pip install cython
    uv run python scripts/compile_proprietary.py build
```

## 添加新的闭源模块

1. 在 `src/core/proprietary/` 创建 `myalgo.pyx`
2. 实现功能（避免 `async def`，Cython 对 async 支持有限）
3. 在 `src/core/proprietary/__init__.py` 注册导出
4. 在 `src/services/` 对应 stub 文件中重新导出
5. 本地执行 `python scripts/compile_proprietary.py build`
6. 提交新的 `.so` 文件到 git
