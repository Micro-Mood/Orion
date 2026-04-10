# Orion

<div align="center">

<h3>🌌 Open-Source AI Coding Agent — Your Self-Hosted Copilot</h3>

**An AI assistant that can read, write, search, and run your code — powered by any LLM**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20macOS-blue.svg)]()
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)

[**中文文档**](README_CN.md)

</div>

---

## ✨ What is Orion?

Orion is an open-source AI coding agent — think **GitHub Copilot + Cursor**, but fully self-hosted, model-agnostic, and running in your browser.

You give it a task ("refactor this module", "add unit tests", "find and fix the bug"). It reads your codebase, plans a solution, edits files, runs commands, and iterates — just like an AI-powered developer sitting next to you.

- **No vendor lock-in** — bring any OpenAI-compatible model (Qwen, DeepSeek, GPT, Claude, etc.)
- **No IDE plugin required** — works in any browser, on any device
- **Full autonomy** — the AI doesn't just suggest code, it executes: reads files, writes code, runs tests, searches symbols

<div align="center">

| Feature | Description |
|---------|-------------|
| 🤖 **Agentic Coding** | AI autonomously reads, edits, searches, and runs code in a tool loop |
| 🛠️ **28 Built-in Tools** | File ops, command execution, code search — via [Axon](https://github.com/Micro-Mood/Axon) MCP Server |
| 🔄 **Streaming Responses** | Real-time output with smart JSON/text detection |
| 🧠 **Two-Phase Tool Calling** | SELECT → PARAMS → EXEC loop — saves 60-80% tokens vs. full schema injection |
| 📉 **Auto Model Fallback** | FIFO model degradation (e.g. flash → turbo → plus) on failure |
| 💬 **Multi-Session Chat** | Multiple conversations with full persistent history and context |
| 📁 **Workspace Browser** | Built-in file explorer with real-time filesystem monitoring |
| 🔐 **Auth & Security** | JWT + bcrypt auth, path boundary enforcement, dangerous command blocking |
| 🎨 **VS Code-Style UI** | Dark IDE-style interface, responsive for desktop and mobile |
| 🌐 **Any LLM** | Qwen, DeepSeek, Kimi, OpenAI, Claude — any OpenAI-compatible API |

</div>

## 🏗️ Architecture

```
┌─────────────────────────────────────────┐
│  Web UI                                 │
│  Vue 3 · WebSocket · Markdown · Hljs    │
├─────────────────────────────────────────┤
│  FastAPI Server                         │
│  Auth · WebSocket · Static · FS Watch   │
├─────────────────────────────────────────┤
│  Orion Engine                           │
│  SELECT → PARAMS → EXEC tool loop      │
│  Streaming · Cancel · Context FIFO      │
├──────────────────┬──────────────────────┤
│  LLM Client      │  MCP Client (TCP)   │
│  OpenAI-compat   │  JSON-RPC 2.0       │
│  Model fallback  │                     │
└──────────────────┴──────────────────────┤
                   │  Axon MCP Server     │
                   │  (Git Submodule)     │
                   └──────────────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Git (for submodule)

### 1. Clone with submodule

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` — at minimum, set your LLM API key:

```json
{
    "llm": {
        "api_key": "sk-your-api-key",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
    }
}
```

Or use environment variables:

```bash
export ORION_API_KEY="sk-your-api-key"
export ORION_API_URL="https://api.openai.com/v1"  # or any compatible endpoint
```

### 4. Run

```bash
cd src
python main.py
```

Open `http://127.0.0.1:8080` in your browser. On first visit, you'll set a login password.

## ⚙️ Configuration

Configuration is loaded with priority: **Environment Variables > config.json > Defaults**

### config.json

| Section | Field | Default | Description |
|---------|-------|---------|-------------|
| `llm` | `api_key` | `""` | LLM API key |
| `llm` | `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI-compatible endpoint |
| `llm` | `models` | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | Model list (FIFO fallback order) |
| `llm` | `temperature` | `0.7` | Sampling temperature |
| `llm` | `timeout` | `120` | Request timeout (seconds) |
| `axon` | `host` | `127.0.0.1` | Axon MCP Server host |
| `axon` | `port` | `9100` | Axon MCP Server port |
| `axon` | `workspace` | `""` | Working directory for Axon (defaults to engine's) |
| `engine` | `max_history` | `20` | Max context messages (FIFO sliding window) |
| `engine` | `max_iterations` | `30` | Max tool-call iterations per message |
| `engine` | `working_directory` | `""` | Working directory (defaults to `workspace/`) |
| `server` | `host` | `127.0.0.1` | Server bind address |
| `server` | `port` | `8080` | Server port |

### Environment Variables

| Variable | Maps to |
|----------|---------|
| `ORION_API_KEY` | `llm.api_key` |
| `ORION_API_URL` | `llm.base_url` |
| `ORION_TEMPERATURE` | `llm.temperature` |
| `ORION_AXON_HOST` | `axon.host` |
| `ORION_AXON_PORT` | `axon.port` |
| `ORION_AXON_WORKSPACE` | `axon.workspace` |
| `ORION_MAX_HISTORY` | `engine.max_history` |
| `ORION_MAX_ITERATIONS` | `engine.max_iterations` |
| `ORION_WORKING_DIR` | `engine.working_directory` |
| `ORION_HOST` | `server.host` |
| `ORION_PORT` | `server.port` |

## 🛠️ Tools

Orion has access to **28 tools** provided by [Axon MCP Server](https://github.com/Micro-Mood/Axon):

### File Operations (14)
`read_file` · `write_file` · `create_file` · `delete_file` · `copy_file` · `move_file` · `create_directory` · `delete_directory` · `move_directory` · `list_directory` · `stat_path` · `insert_text` · `replace_range` · `delete_range`

### Command Execution (10)
`run_command` · `create_task` · `stop_task` · `del_task` · `task_status` · `list_tasks` · `read_stdout` · `read_stderr` · `write_stdin` · `wait_task`

### Search (3)
`find_files` · `search_text` · `find_symbol`

### System (1)
`get_system_info`

## 📁 Project Structure

```
Orion/
├── config.example.json     # Configuration template
├── requirements.txt        # Python dependencies
├── axon/                   # Axon MCP Server (git submodule)
├── src/
│   ├── main.py             # Entry point — starts Axon + Uvicorn
│   ├── server.py           # FastAPI + WebSocket server
│   ├── engine.py           # AI engine (SELECT → PARAMS → EXEC loop)
│   ├── llm.py              # LLM client (OpenAI-compatible, model fallback)
│   ├── mcp_client.py       # MCP TCP client (JSON-RPC 2.0)
│   ├── axon_manager.py     # Axon subprocess lifecycle management
│   ├── config.py           # Configuration management (singleton)
│   ├── context.py          # Conversation context (FIFO sliding window)
│   ├── prompt.py           # System prompt builder
│   ├── store.py            # Session & message persistence (JSON files)
│   ├── tools.py            # Tool registry (28 tools + control commands)
│   ├── prompts/
│   │   └── system.md       # System prompt template
│   └── web/                # Frontend (Vue 3 SPA)
│       ├── index.html
│       ├── app.js
│       └── style.css
├── data/                   # Runtime data (auto-created, gitignored)
│   ├── sessions.json
│   └── messages/
├── workspace/              # Default working directory (gitignored)
└── docs/                   # Documentation
```

## 🔒 Security

- **Password authentication** — bcrypt-hashed passwords, JWT tokens
- **Path boundary enforcement** — Axon restricts file operations to the workspace
- **Dangerous command blocking** — 50+ patterns blocked by Axon middleware
- **No sensitive data in repo** — API keys, passwords, and JWT secrets are in `config.json` (gitignored)

## 💡 Why Orion?

| | GitHub Copilot | Cursor | **Orion** |
|---|---|---|---|
| Self-hosted | ❌ | ❌ | ✅ |
| Model freedom | ❌ GPT/Claude only | Partial | ✅ Any OpenAI-compatible |
| Open source | ❌ | ❌ | ✅ MIT |
| Agentic (edits files) | ✅ | ✅ | ✅ |
| Browser-based | ❌ IDE plugin | ❌ Desktop app | ✅ Any browser |
| No subscription | ❌ | ❌ | ✅ Pay only for API usage |

## 🤝 Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## 📄 License

[MIT](LICENSE)
