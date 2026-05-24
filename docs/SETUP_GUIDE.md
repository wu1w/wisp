# Wisp 快速上手指南

> 从零开始，5 分钟跑起来。

---

## 📋 目录

1. [这是什么项目](#1-这是什么项目)
2. [环境准备](#2-环境准备)
3. [克隆项目](#3-克隆项目)
4. [配置 LLM（语言模型）](#4-配置-llm语言模型)
5. [配置 Embedding（向量模型）](#5-配置-embedding向量模型)
6. [配置数据库和 Redis](#6-配置数据库和-redis)
7. [安装依赖](#7-安装依赖)
8. [编译核心模块](#8-编译核心模块)
9. [初始化数据库](#9-初始化数据库)
10. [启动服务](#10-启动服务)
11. [常见问题](#11-常见问题)

---

## 1. 这是什么项目

Wisp 是一个**AI 编程助手**，可以帮你：

- 自动阅读和修改代码文件
- 执行 Bash 命令
- 搜索记忆（之前做过的操作）
- 思考并规划复杂任务
- 调用各种工具（文件操作、代码执行等）

它支持多个 LLM 提供商（OpenAI、MiniMax、Claude、Ollama），你可以根据自己的需要选择。

---

## 2. 环境准备

### 2.1 需要准备什么

| 项目 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.10 或更高 | 建议 3.11 / 3.12 / 3.14 |
| Git | 任意版本 | 用于下载代码 |
| PostgreSQL | 14 或更高 | 存储任务和数据 |
| Redis | 6 或更高 | 消息队列 |

### 2.2 检查你的环境

打开终端，输入：

```bash
python3 --version
git --version
```

如果显示类似 `Python 3.11.0` 和 `git version 2.x.x` 就没问题。

> 💡 **没有 Python？** 前往 https://www.python.org/downloads/ 下载安装。Windows 用户建议用 [Python 3.11 installer](https://www.python.org/ftp/python/3.11.0/python-3.11.0-amd64.exe)。

> 💡 **没有 Git？** 前往 https://git-scm.com/download 下载安装。

---

## 3. 克隆项目

打开终端，执行：

```bash
git clone https://github.com/你的用户名/wisp.git
cd wisp
```

> ⚠️ **注意**：把 `你的用户名` 换成你实际的 GitHub 用户名。

如果你是第一次克隆，需要先初始化：

```bash
cd wisp
git init
git remote add origin https://github.com/你的用户名/wisp.git
```

---

## 4. 配置 LLM（语言模型）

LLM 是 Wisp 的"大脑"，你需要给它配置一个 AI 模型。

### 4.1 LLM 提供商对比

| 提供商 | 模型 | 特点 | 推荐度 |
|--------|------|------|--------|
| **MiniMax** | MiniMax-M2.7 | 中文优化，便宜 | ⭐⭐⭐⭐⭐ |
| **OpenAI** | GPT-4o / GPT-4o-mini | 通用强大 | ⭐⭐⭐⭐ |
| **Anthropic** | Claude 3.5 Sonnet | 推理能力强 | ⭐⭐⭐⭐ |
| **Ollama** | Llama3 / Qwen | 本地运行，完全免费 | ⭐⭐⭐ |

### 4.2 获取 API Key

**MiniMax（推荐，国内可用）**
1. 访问 https://www.minimaxi.com/
2. 注册账号并登录
3. 进入控制台 → API Key → 创建新 Key
4. 复制 Key，格式类似 `sk-cp-xxxxxxxxxxxxxxxx`

**OpenAI**
1. 访问 https://platform.openai.com/
2. 注册账号并登录
3. 进入 API Keys → Create new secret key
4. 复制 Key，格式类似 `sk-xxxxxxxxxxxxxxxx`

**Anthropic**
1. 访问 https://www.anthropic.com/
2. 注册账号并登录
3. 进入 Console → API Keys → Create Key
4. 复制 Key，格式类似 `sk-ant-xxxxxxxxxxxxxxxx`

### 4.3 填写配置文件

在项目根目录创建 `.env` 文件：

```bash
cp .env.example .env
```

用文本编辑器打开 `.env` 文件，填写你的 API Key：

```bash
# 打开编辑
nano .env
# 或用 VS Code
code .env
```

找到对应行，填入你的 Key：

```bash
# MiniMax（推荐）
MINIMAX_API_KEY=你复制的Key

# 或 OpenAI
OPENAI_API_KEY=你复制的Key

# 或 Anthropic
ANTHROPIC_API_KEY=你复制的Key
```

> ⚠️ **安全提醒**：你的 API Key 就是你的账号通行证，**千万不要**提交到 GitHub！`.env` 文件已经被 `.gitignore` 忽略了，不会被上传。

### 4.4 选择使用哪个模型

打开 `config/default.yaml`，找到 `llm` 部分：

```yaml
llm:
  default_provider: "minimax"   # 默认使用 MiniMax

  profiles:
    coding:                    # 编程模式
      provider: "minimax"
      model: "MiniMax-M2.7"   # 模型名
      temperature: 0.1         # 创造性（0-1，越低越稳定）
      max_tokens: 4096         # 最大回复长度
      fallback_provider: "openai"  # 如果 MiniMax 失败，备用

    chatting:                  # 聊天模式
      provider: "ollama"
      model: "llama3"

    cheap:                     # 省钱模式
      provider: "openai"
      model: "gpt-4o-mini"
```

**常见配置示例**：

```yaml
# 方案 A：只用 MiniMax
llm:
  profiles:
    coding:
      provider: "minimax"
      model: "MiniMax-M2.7"
      temperature: 0.1
      max_tokens: 4096

# 方案 B：MiniMax 为主，OpenAI 备用
llm:
  profiles:
    coding:
      provider: "minimax"
      model: "MiniMax-M2.7"
      temperature: 0.1
      fallback_provider: "openai"
      fallback_model: "gpt-4o-mini"

# 方案 C：只用 OpenAI
llm:
  profiles:
    coding:
      provider: "openai"
      model: "gpt-4o"
      temperature: 0.1
```

---

## 5. 配置 Embedding（向量模型）

Embedding 是什么？它把你的文本转换成"数字"（向量），让 Wisp 能够理解语义、搜索相关内容。比如你说"查找文件操作"，Wisp 能理解你其实想找"读写文件"相关的记忆。

### 5.1 Embedding 提供商对比

| 提供商 | 模型 | 维度 | 特点 |
|--------|------|------|------|
| **MiniMax** | embo-01 | 1024 | 高质量，免费额度 |
| **SiliconFlow** | BAAI/bge-small-zh-v1.5 | 384 | 免费，中文优化 |
| **OpenAI** | text-embedding-3-small | 1536 | 通用，量大 |

### 5.2 配置 Embedding

Embedding 可以在 `.env` 中配置，也可以用默认的 MiniMax。

如果需要切换，打开 `config/default.yaml`：

```yaml
llm:
  providers:
    minimax:
      api_key: "${MINIMAX_API_KEY}"
      base_url: "https://api.minimaxi.com/v1"

    siliconflow:
      api_key: "${SILICONFLOW_API_KEY}"   # 需要在 .env 中设置

  # 这是关键配置！指定使用哪些 Embedding 提供商
  embedding:
    chain:
      - minimax       # 第一选择：MiniMax（有免费额度）
      - siliconflow   # 备用：SiliconFlow（免费）
      # - openai      # 如果需要，可以加 OpenAI
```

> 💡 **chain 的含义**：Wisp 会按顺序尝试每个提供商。如果 MiniMax 可用，就用 MiniMax；如果不可用（余额不足等），自动切换到 SiliconFlow。

### 5.3 SiliconFlow（可选，免费）

如果你的 MiniMax 额度用完了，可以申请 SiliconFlow：

1. 访问 https://www.siliconflow.cn/
2. 注册并获取 API Key
3. 在 `.env` 中添加：
   ```bash
   SILICONFLOW_API_KEY=你复制的Key
   ```

---

## 6. 配置数据库和 Redis

Wisp 需要 PostgreSQL 存储任务数据，Redis 处理消息队列。

### 6.1 使用腾讯云预置数据库（已有）

如果你有腾讯云 PostgreSQL，直接填写 `.env`：

```bash
DATABASE_HOST=你的数据库地址
DATABASE_USER=你的用户名
DATABASE_PASSWORD=你的密码
```

### 6.2 本地安装 PostgreSQL 和 Redis

**macOS（使用 Homebrew）**：

```bash
brew install postgresql@14 redis
brew services start postgresql@14
brew services start redis
```

**Ubuntu / Debian**：

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib redis-server

# 启动服务
sudo systemctl start postgresql
sudo systemctl start redis-server
```

**Windows**：建议用 Docker，或者下载 https://www.postgresql.org/download/windows/

### 6.3 创建数据库

登录 PostgreSQL，创建数据库和用户：

```bash
# 登录
psql -U postgres

# 在 PostgreSQL 命令行中执行：
CREATE USER wisp WITH PASSWORD '你的密码';
CREATE DATABASE wisp OWNER wisp;
GRANT ALL PRIVILEGES ON DATABASE wisp TO wisp;
\q
```

然后在 `.env` 中填写：

```bash
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=wisp
DATABASE_USER=wisp
DATABASE_PASSWORD=你的密码
REDIS_HOST=localhost
REDIS_PORT=6379
```

---

## 7. 安装依赖

### 7.1 创建虚拟环境（推荐）

```bash
# 创建虚拟环境
python3 -m venv .venv

# 激活虚拟环境
# macOS / Linux:
source .venv/bin/activate

# Windows:
.venv\Scripts\activate
```

> 💡 **什么是虚拟环境？** 它是一个独立的 Python 环境，不会影响你系统自带的 Python。建议每个项目都使用虚拟环境。

### 7.2 安装 Python 包

```bash
pip install -r requirements.txt
```

或者使用更快的 `uv`：

```bash
pip install uv
uv sync
```

---

## 8. 编译核心模块

Wisp 的核心算法是闭源的，已经编译成 `.so` 文件。大多数情况下不需要重新编译。

### 8.1 如果 .so 文件已存在

直接跳过这步，`.so` 文件已经随项目提供了。

### 8.2 如果需要重新编译

> ⚠️ **只有**当你遇到 `ModuleNotFoundError: No module named 'src.core.proprietary.etl'` 时才需要这步。

```bash
# 1. 安装 Cython
pip install cython

# 2. 编译
python scripts/compile_proprietary.py build

# 3. 验证
python scripts/compile_proprietary.py verify

# 应该看到：
# Build complete: 2 .so files
# Import test PASSED
```

---

## 9. 初始化数据库

### 9.1 创建扩展

Wisp 使用 `pg_trgm` 做模糊搜索，`vector` 做向量存储。先创建扩展：

```bash
psql -U wisp -d wisp -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
psql -U wisp -d wisp -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 9.2 运行数据库迁移

```bash
alembic upgrade head
```

> 💡 **Alembic 是什么？** 它是数据库迁移工具，会自动帮你创建所有需要的表。

### 9.3 验证数据库

```bash
psql -U wisp -d wisp -c "\dt"
```

应该看到类似：

```
               List of relations
 Schema |       Name        | Type  |  Owner
--------+------------------+-------+--------
 public | agent_checkpoints| table | wisp
 public | evolution_outcomes | table | wisp
 public | memories          | table | wisp
 public | tasks             | table | wisp
 public | tool_executions   | table | wisp
(12 rows)
```

---

## 10. 启动服务

### 10.1 方式一：直接运行（开发用）

```bash
# 确保虚拟环境已激活
source .venv/bin/activate

# 启动 API 服务器
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

看到类似输出就是成功了：

```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
```

### 10.2 方式二：Docker（生产用）

```bash
# 构建并启动所有服务
docker-compose -f docker/docker-compose.yml up -d

# 查看日志
docker-compose -f docker/docker-compose.yml logs -f
```

### 10.3 验证运行

打开浏览器访问：

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

---

## 11. 常见问题

### Q1：启动时报 `ModuleNotFoundError: No module named 'asyncpg'`

```bash
pip install asyncpg
```

### Q2：启动时报 `ModuleNotFoundError: No module named 'src.core.proprietary.etl'`

需要重新编译 `.so` 文件：

```bash
pip install cython
python scripts/compile_proprietary.py build
python scripts/compile_proprietary.py verify
```

### Q3：`alembic upgrade head` 报错 `relation "alembic_version" does not exist`

正常，先初始化：

```bash
alembic stamp head
alembic upgrade head
```

### Q4：数据库连接失败 `connection refused`

检查 PostgreSQL 是否运行：

```bash
# macOS
brew services list

# Linux
sudo systemctl status postgresql
```

### Q5：LLM 调用失败 `401 Unauthorized`

检查 `.env` 中的 API Key 是否正确，是否有多余的空格。

### Q6：Embedding 返回 `None`

1. 检查 MiniMax API Key 是否配置
2. 检查余额是否充足
3. 参考第 5 节配置 SiliconFlow 作为备用

### Q7：内存不足 `out of memory`

减小 `config/default.yaml` 中的批处理大小，或增加 Redis 内存限制。

---

## 🎉 恭喜！

到这里，你应该已经成功运行 Wisp 了。如果还有问题，请提交 GitHub Issue，我会尽快回复。

**下一步建议**：

1. 试试调用 API：`curl http://localhost:8000/health`
2. 查看 API 文档：http://localhost:8000/docs
3. 配置你喜欢的 LLM 提供商
