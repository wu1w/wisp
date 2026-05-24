# Wisp — AI Coding Agent

A self-hosted AI coding agent with multi-provider LLM support, built-in tool execution, and automatic prompt evolution.

## Features

- 🤖 **Multi-Provider LLM Gateway** — OpenAI, MiniMax, Anthropic, Ollama
- 🔧 **7 Built-in Tools** — bash, read_file, write_file, list_dir, search_memory, reflect_on_error, save_memory
- 🔄 **Circuit Breaker** — Dead-loop detection with automatic panic recovery
- 🧠 **Memory System** — ETL pipeline with semantic search (Hybrid RRF + pgvector)
- 📈 **Prompt Evolution** — Data-driven prompt improvement engine
- 🐳 **Container-Ready** — Docker Compose deployment
- 🪟 **Windows Support** — Native `.exe` build available

---

## Quick Start

### Linux / macOS

```bash
git clone https://github.com/wu1w/wisp.git
cd wisp

# 安装依赖
pip install -e .

# 配置（复制并填写 API Key）
cp .env.example .env
nano .env

# 启动
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

### Windows（下载预编译 exe）

> ⚠️ **需要 Docker Desktop for Windows**（Wisp Worker 需要 Docker 来执行命令）

1. 从 [Releases](https://github.com/wu1w/wisp/releases) 下载最新的 `wisp-windows-x64.exe`
2. 创建 `.env` 文件（参考 `.env.example`）
3. 确保 Docker Desktop 在运行
4. 双击运行或命令行启动：
   ```cmd
   set DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/wisp
   set REDIS_URL=redis://localhost:6379
   set MINIMAX_API_KEY=your_key_here
   wisp-windows-x64.exe
   ```

---

## Windows 开发构建（从源码）

如果你想从源码构建 Windows 版本，需要：

### 前置条件

- **Python 3.11**（[下载地址](https://www.python.org/ftp/python/3.11.0/python-3.11.0-amd64.exe)）
- **Git**（[下载地址](https://git-scm.com/download/win)）
- **Docker Desktop for Windows**（[下载地址](https://www.docker.com/products/docker-desktop/)）

### 构建步骤

```cmd
# 1. 克隆代码
git clone https://github.com/wu1w/wisp.git
cd wisp

# 2. 安装 Python 依赖
pip install pyinstaller
pip install -e .

# 3. 编译打包
pyinstaller wisp.spec --clean --noconfirm

# 4. 产物在 dist\wisp\wisp.exe
dist\wisp\wisp.exe
```

> 💡 **为什么 Docker Desktop 必须运行？** Wisp 的 Worker 通过 Docker API（`npipe:////./pipe/docker_engine`）执行命令（如 `bash`、`read_file` 等）。没有 Docker Desktop，Worker 无法工作。

### GitHub Actions 自动构建

每次推送新 tag（如 `v0.1.0`），会自动构建 Windows exe：

```yaml
# 触发条件：推送 v* tag
git tag v0.1.0
git push origin v0.1.0
```

构建产物会在 [Releases](https://github.com/wu1w/wisp/releases) 页面自动发布。

---

## 配置 LLM

### 选择提供商

编辑 `config/default.yaml` 中的 `llm.providers`：

```yaml
llm:
  default_provider: "minimax"   # 推荐国内用户

  profiles:
    coding:
      provider: "minimax"
      model: "MiniMax-M2.7"
      temperature: 0.1
      fallback_provider: "openai"
```

### 支持的提供商

| 提供商 | 模型 | API Key 获取 |
|--------|------|-------------|
| MiniMax | MiniMax-M2.7 | [minimaxi.com](https://www.minimaxi.com/) |
| OpenAI | GPT-4o, GPT-4o-mini | [platform.openai.com](https://platform.openai.com/) |
| Anthropic | Claude 3.5 Sonnet | [anthropic.com](https://www.anthropic.com/) |
| Ollama | llama3, qwen | [本地运行](https://ollama.com/)（免费） |

---

## 环境变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+asyncpg://user:pass@host:5432/wisp` |
| `REDIS_URL` | Redis 连接串 | `redis://localhost:6379` |
| `MINIMAX_API_KEY` | MiniMax API Key | `sk-xxx…` |
| `OPENAI_API_KEY` | OpenAI API Key | `sk-xxx…` |
| `ANTHROPIC_API_KEY` | Anthropic API Key | `sk-ant-xxx…` |

---

## 架构

```
Agent Core (Open Source)
├── src/core/agent.py         — 状态机
├── src/core/llm/            — LLM 抽象层（支持多 Provider）
├── src/core/tools.py         — 工具注册表
└── src/services/             — Worker, Scheduler, Redis Streams

Proprietary Modules (.so — see docs/PROPRIETARY_BUILD.md)
├── src/core/proprietary/etl.pyx       — ETL 流水线（闭源）
└── src/core/proprietary/evolution.pyx  — Prompt 进化引擎（闭源）
```

---

## License

Apache 2.0 + 闭源核心模块。详见 [docs/PROPRIETARY_BUILD.md](docs/PROPRIETARY_BUILD.md)。
