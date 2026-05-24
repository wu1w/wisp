# Wisp — AI Coding Agent

A self-hosted AI coding agent with multi-provider LLM support, built-in tool execution, and automatic prompt evolution.

## Features

- 🤖 **Multi-Provider LLM Gateway** — OpenAI, MiniMax, Anthropic, Ollama
- 🔧 **7 Built-in Tools** — bash, read_file, write_file, list_dir, search_memory, reflect_on_error, save_memory
- 🔄 **Circuit Breaker** — Dead-loop detection with automatic panic recovery
- 🧠 **Memory System** — ETL pipeline with semantic search (Hybrid RRF + pgvector)
- 📈 **Prompt Evolution** — Data-driven prompt improvement engine
- 🐳 **Container-Ready** — Docker Compose deployment

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/yourname/wisp.git
cd wisp
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API keys and database credentials
```

### 3. Build Closed-Source Modules

> ⚠️ **Note:** Core algorithms are compiled to binary `.so` files. You must rebuild these for your Python version:

```bash
pip install cython
python scripts/compile_proprietary.py build
python scripts/compile_proprietary.py verify
```

See [docs/PROPRIETARY_BUILD.md](docs/PROPRIETARY_BUILD.md) for multi-version build instructions.

### 4. Run

```bash
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000
```

## Architecture

```
Agent Core (Open Source)
├── src/core/agent.py         — State machine (IDLE→THINKING→TOOL_CALLING→DONE)
├── src/core/llm/            — LLM Provider abstraction layer
├── src/core/tools.py        — Tool registry
└── src/services/            — Worker, Scheduler, Redis Streams

Proprietary Modules (Compiled to .so — see docs/PROPRIETARY_BUILD.md)
├── src/core/proprietary/etl.pyx       — ETL pipeline & deduplication
└── src/core/proprietary/evolution.pyx  — Prompt evolution analysis
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_HOST` | PostgreSQL host |
| `DATABASE_USER` | PostgreSQL user |
| `DATABASE_PASSWORD` | PostgreSQL password |
| `MINIMAX_API_KEY` | MiniMax API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `REDIS_PASSWORD` | Redis password |

See `.env.example` for the full list.

## License

This project contains both open-source (Apache 2.0) and proprietary components. See `docs/PROPRIETARY_BUILD.md` for details.
