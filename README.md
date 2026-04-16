# Orion

<div align="center">

<h3>🌌 An AI that actually does things, not just talks</h3>

**A $1.50/month server + free LLM = your personal AI assistant, always online, never forgets.**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20macOS-blue.svg)]()
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)

[**中文文档**](README_CN.md)

</div>

---

## What is Orion?

ChatGPT, Kimi, Claude — they're powerful, but they share one fatal flaw: **they forget everything.**

You spend an hour brainstorming with an AI, organize your thoughts, make plans… then close the tab and it's all gone. It doesn't remember what you said last month. It can't touch your files.

**Orion is different.** It reads files, writes files, creates folders, runs scripts, searches content, fetches web pages — then saves everything as your own files. Next time you ask, it just reads your files.

> **Files are memory. Not some mysterious "memory feature" — just plain Markdown files you can open, edit, and search.**

## What does it feel like to use?

```
You: "Save this reflection I just wrote"
AI: [create file] Saved to /reflections.md

(two weeks later)

You: "What did I write before?"
AI: [read file] You have 3 reflection entries. The most recent one is about...
```

```
You: "I'm reading""Nonviolent Communication", save today's insight"
AI: [write file] Appended to /books/nonviolent-communication.md

You: "Where was that insight about 'intent ≠ impact'?"
AI: [search files] In your April 11th notes...
```

```
You: "Organize my notes by topic"
AI: [list directory] Found 47 files
    [read each one] Analyzing content...
    [create folders] Created 6 topic folders
    [move files] All sorted
AI: "47 notes organized into 6 categories."
```

**It's not just "answering questions" — it's doing work for you.**

## Why not just use ChatGPT?

| | ChatGPT / Kimi / Claude | **Orion** |
|---|---|---|
| **Memory** | Black-box "memory" — who knows what it stores | **Your own files** — visible, editable, yours |
| **Can it act?** | Can only *suggest* what to do | **Actually does it** — reads/writes files, runs commands, iterates |
| **Your data** | On their servers | **On your machine** |
| **Model** | Locked to one provider | **Swap freely** — Qwen, DeepSeek, GPT, Claude, whatever |
| **Monthly cost** | ChatGPT Plus $20/mo | **~$1.50 server + free model** (Qwen Flash free tier covers daily use) |
| **Open source** | ❌ | ✅ MIT — do whatever you want |

> 💡 **Cost breakdown**: Grab a cheap VPS (~$1.50/month), install Orion, hook up Qwen Flash (free), and you've got a 24/7 AI assistant accessible from anywhere on your phone.
>
> Less than a cup of coffee. ChatGPT Plus money lasts you a year.

## What can it do?

- **🧠 Personal assistant** — capture thoughts, organize reflections, track goals. You talk, it remembers, forever
- **📚 Reading notes** — discuss books with AI, save insights to files, review anytime
- **📋 List management** — TODOs, subscriptions, expenses — one sentence and it's done
- **🗂️ File organization** — "Sort my notes by topic" — it reads, categorizes, and moves them on its own
- **💻 Coding** — read code, edit code, run scripts, debug. It's a full coding agent too
- **🌐 Research** — fetch web pages, summarize content into files
- **📊 Data processing** — analyze CSV/JSON, run Python, generate reports

## Screenshots

<div align="center">

<img src="docs/image/desktop.png" width="800" alt="Orion Desktop UI">
<p><b>Desktop — File Browser + Code Editor + AI Chat</b></p>

<table>
<tr>
<td><img src="docs/image/mobile-chat.png" width="260" alt="Mobile Chat"></td>
<td><img src="docs/image/mobile-editor.png" width="260" alt="Mobile Editor"></td>
<td><img src="docs/image/mobile-files.png" width="260" alt="Mobile Files"></td>
</tr>
<tr>
<td align="center"><b>AI Chat</b></td>
<td align="center"><b>Code Editor</b></td>
<td align="center"><b>File Browser</b></td>
</tr>
</table>

</div>

## How does it work?

Orion has 27 tools (read files, write files, run commands, search…), and the AI decides which ones to use:

```
You say something
 ↓
AI picks tools → fills params → executes → reads results → decides next step
 ↓
Loop until done
```

This two-phase tool calling saves 60-80% tokens compared to full schema injection. In plain English: **faster and cheaper.**

<details>
<summary><b>Architecture (for the technical crowd)</b></summary>

```
┌─────────────────────────────────────────┐
│  Web UI                                 │
│  Vue 3 · WebSocket · Markdown · CM6     │
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

</details>

### Features at a Glance

| | |
|---|---|
| 🧠 **Two-Phase Tool Calling** | Saves 60-80% tokens vs. full schema injection |
| 📉 **Auto Model Fallback** | Cheap model first, upgrade on failure |
| 🔄 **Streaming Responses** | Real-time output, no waiting |
| 💬 **Multi-Session** | Parallel conversations with full history |
| 📁 **File Browser** | VS Code-style, real-time filesystem monitoring |
| ✏️ **Code Editor** | CodeMirror 6, syntax highlighting for 13+ languages |
| 💭 **Thinking Display** | See what the AI is thinking (for supported models) |
| 🔐 **Authentication** | JWT + bcrypt, safe for public deployment |
| 🎨 **Dark Theme** | The kind developers actually enjoy |

## Quick Start

### Prerequisites

- Python 3.10+
- Git

### 1. Clone

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
```

Missed the submodule?

```bash
git submodule update --init
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
pip install -r axon/requirements.txt
```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your API key:

```json
{
    "llm": {
        "api_key": "sk-your-api-key",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
    }
}
```

> Get a free Qwen API key from [Alibaba Cloud Bailian](https://bailian.console.aliyun.com/). The Flash model has a generous free tier.

Or use environment variables:

```bash
export ORION_API_KEY="sk-your-api-key"
```

### 4. Run

```bash
cd src
python main.py
```

Open `http://127.0.0.1:8080`, set a password, start chatting.

## Deploy to a Server (the $1.50/month plan)

Want access from anywhere? Grab a cheap VPS and:

```bash
# On your server
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
pip install -r requirements.txt
pip install -r axon/requirements.txt
cp config.example.json config.json
# Edit config.json, add your API key

# Bind to all interfaces
export ORION_HOST="0.0.0.0"
cd src && python main.py
```

Set up Nginx reverse proxy + HTTPS and you can access your AI from your phone anywhere.

> The frontend auto-detects its base path, so it works behind any URL prefix (e.g. `https://yourdomain.com/orion/`).

See [docs/getting-started.md](docs/getting-started.md#remote-access) for detailed deployment guide.

## Configuration Reference

Priority: **Environment Variables > config.json > Defaults**

<details>
<summary><b>config.json fields</b></summary>

| Section | Field | Default | Description |
|---------|-------|---------|-------------|
| `llm` | `api_key` | `""` | LLM API key |
| `llm` | `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI-compatible endpoint |
| `llm` | `models` | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | Model list (cheap first) |
| `llm` | `temperature` | `0.7` | Sampling temperature |
| `llm` | `timeout` | `120` | Request timeout (seconds) |
| `axon` | `host` | `127.0.0.1` | Axon MCP Server host |
| `axon` | `port` | `9100` | Axon MCP Server port |
| `axon` | `workspace` | `""` | Working directory |
| `engine` | `max_history` | `20` | Context message count |
| `engine` | `max_iterations` | `30` | Max tool-call rounds per message |
| `engine` | `read_file_max_lines` | `200` | Default max lines for file reads |
| `engine` | `working_directory` | `""` | Working directory (defaults to `workspace/`) |
| `server` | `host` | `127.0.0.1` | Bind address |
| `server` | `port` | `8080` | Port |

</details>

<details>
<summary><b>Environment variables</b></summary>

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

</details>

## 27 Built-in Tools

Provided by [Axon MCP Server](https://github.com/Micro-Mood/Axon):

| Category | Tools |
|----------|-------|
| **Files** (12) | `read_file` · `write_file` · `delete_file` · `copy_file` · `move_file` · `create_directory` · `delete_directory` · `move_directory` · `list_directory` · `stat_path` · `replace_string_in_file` · `multi_replace_string_in_file` |
| **Commands** (10) | `run_command` · `create_task` · `stop_task` · `del_task` · `task_status` · `list_tasks` · `read_stdout` · `read_stderr` · `write_stdin` · `wait_task` |
| **Search** (3) | `find_files` · `search_text` · `find_symbol` |
| **System** (1) | `get_system_info` |
| **Web** (1) | `fetch_webpage` |

## Project Structure

```
Orion/
├── config.example.json     # Config template
├── requirements.txt        # Python dependencies
├── axon/                   # Axon MCP Server (git submodule)
├── src/
│   ├── main.py             # Entry point
│   ├── server.py           # FastAPI + WebSocket
│   ├── engine.py           # AI engine (tool loop)
│   ├── llm.py              # LLM client (model fallback)
│   ├── mcp_client.py       # MCP TCP client
│   ├── axon_manager.py     # Axon subprocess management
│   ├── config.py           # Configuration
│   ├── context.py          # Conversation context
│   ├── prompt.py           # System prompt
│   ├── store.py            # Session persistence
│   ├── tools.py            # Tool registry
│   ├── prompts/
│   │   └── system.md       # System prompt template
│   └── web/                # Frontend
├── data/                   # Runtime data (gitignored)
├── workspace/              # Default working directory (gitignored)
└── docs/
```

## Security

- **Password auth** — bcrypt + JWT
- **Path sandboxing** — file operations restricted to workspace
- **Dangerous command blocking** — 50+ patterns auto-blocked
- **Sensitive data isolation** — keys stay in `config.json` (gitignored)

## Contributing

Issues and PRs welcome!

## License

[MIT](LICENSE) — do whatever you want.
