# Architecture

## Overview

Orion is a self-hosted AI assistant with a layered architecture. Each layer has a single responsibility and communicates through well-defined interfaces.

```
┌─────────────────────────────────────────────────────┐
│  Layer 5: Web UI                                    │
│  Vue 3 SPA · WebSocket · Markdown · CodeMirror 6   │
├─────────────────────────────────────────────────────┤
│  Layer 4: Server                                    │
│  FastAPI · WebSocket Hub · Auth · Static · FS Watch │
├─────────────────────────────────────────────────────┤
│  Layer 3: Engine                                    │
│  Two-Phase Tool Loop · Streaming · Context FIFO     │
├─────────────────┬───────────────────────────────────┤
│  Layer 2a:      │  Layer 2b:                        │
│  LLM Client     │  MCP Client                       │
│  OpenAI-compat  │  JSON-RPC 2.0 over TCP            │
│  Model fallback │                                   │
└─────────────────┴───────────────────────────────────┤
                  │  Layer 1: Axon MCP Server          │
                  │  File · Search · Command · System  │
                  └───────────────────────────────────┘
```

## Components

### Web UI (`src/web/`)

Single-page Vue 3 application with a VS Code-inspired dark theme.

- **No build step** — uses Vue 3 from CDN, no webpack/vite needed
- **WebSocket** — real-time bidirectional communication with the server
- **Code editor** — CodeMirror 6-based editor with syntax highlighting for 13+ languages (local pre-built bundle, zero CDN dependency)
- **Features**: multi-session sidebar, file explorer with Material Icon theme, settings panel, markdown rendering, AI thinking/reasoning display
- **Responsive** — adapts to mobile screens with iOS safe area support

### Server (`src/server.py`)

FastAPI application serving both the API and static files.

- **WebSocket hub** — manages multiple client connections, broadcasts events
- **Authentication** — JWT-based login with bcrypt password hashing; first-visit password setup
- **File system watcher** — monitors the workspace directory using `watchdog`, broadcasts changes to connected clients with debounce
- **Static files** — serves the Vue 3 SPA from `src/web/`

### Engine (`src/engine.py`)

The core AI reasoning loop that processes user messages.

#### Two-Phase Tool Calling

Traditional approaches send the full schema of all tools with every request, consuming significant tokens. Orion uses a two-phase approach:

```
User Message
    │
    ▼
┌─────────┐     AI sees only tool names (compact)
│ SELECT  │──→  AI returns: {"select": ["read_file", "write_file"]}
└────┬────┘
     │
     ▼
┌─────────┐     Inject only selected tools' full parameter schemas
│ PARAMS  │──→  AI returns: {"call": "read_file", "path": "/src/main.py"}
└────┬────┘
     │
     ▼
┌─────────┐     Execute via MCP, inject result back into context
│  EXEC   │──→  Result: {content of the file}
└────┬────┘
     │
     ▼
  Loop back to SELECT (or AI calls "done" to finish)
```

**Benefits**:
- SELECT phase sends only tool names → minimal tokens
- PARAMS phase sends only the schemas for selected tools
- Typically saves 60-80% of tool-description tokens vs. sending all schemas every turn

#### Other Engine Features

- **Streaming** — real-time token delivery during SELECT phase (smart JSON detection to avoid partial JSON streaming)
- **Cancel** — any in-progress generation can be cancelled via WebSocket
- **Persistent context** — all intermediate messages (tool injections, results) are saved to disk, enabling seamless multi-turn conversations
- **Consecutive failure detection** — detects and breaks out of tool-call failure loops
- **PARAMS overflow protection** — prevents excessively large parameter payloads

### LLM Client (`src/llm.py`)

Async HTTP client for OpenAI-compatible APIs.

- **FIFO model degradation** — models are tried in order; on failure, the client falls back to the next model in the list (e.g. `qwen-flash` → `qwen-turbo` → `qwen-plus`)
- **Exponential backoff retry** — configurable max retries
- **Streaming and non-streaming** — both modes supported
- **Token usage tracking** — per-request and cumulative

### MCP Client (`src/mcp_client.py`)

Async TCP client that communicates with Axon MCP Server using JSON-RPC 2.0.

- **Line-delimited protocol** — each JSON message terminated by `\n`
- **Auto-reconnect** — reconnects on connection loss
- **Timeout inference** — automatically sets appropriate timeouts based on the method being called

### Axon Manager (`src/axon_manager.py`)

Manages the Axon MCP Server subprocess lifecycle.

- **Auto-start** — spawns Axon on Orion startup, waits for TCP port readiness
- **External detection** — if Axon is already running (externally managed), uses it without spawning
- **Graceful shutdown** — registered via `atexit` for clean process termination
- **Crash recovery** — monitors the subprocess and can restart on crash

### Session Store (`src/store.py`)

JSON file-based persistence for sessions and messages.

- **Separated storage** — `sessions.json` for metadata, `messages/{id}.json` for content
- **Dual message tracks**:
  - `messages[]` — user-visible messages for the frontend
  - `context[]` — full AI context including tool injections and results
- **Thread-safe** — RLock for concurrent access
- **Atomic writes** — write-to-temp-then-rename pattern prevents data corruption
- **Auto-truncation** — limits on message count and file size

### Context (`src/context.py`)

Manages the conversation context sent to the LLM.

- **FIFO sliding window** — keeps the last N messages, drops oldest when full
- **System message isolation** — system prompt is always first, never trimmed
- **Phase state machine** — tracks SELECT / PARAMS / EXEC phases

### Tool Registry (`src/tools.py`)

Registers all 27 Axon tools + control commands (done, ask, fail).

- **Compact description format** — `name|desc|param:type*desc;...` for token-efficient schema transmission
- **Category-based grouping** — tools organized by category (file, command, search, system)

## Data Flow

### User sends a message

```
Browser                Server              Engine           LLM API         Axon
  │                      │                   │                │              │
  │──WebSocket msg──────▶│                   │                │              │
  │                      │──run()───────────▶│                │              │
  │                      │                   │──chat()───────▶│              │
  │◀─stream delta────────│◀──on_text()───────│◀──stream──────│              │
  │                      │                   │  (SELECT: picks tools)       │
  │                      │                   │──chat()───────▶│              │
  │                      │                   │◀──response─────│              │
  │                      │                   │  (PARAMS: fills params)      │
  │◀─tool_start──────────│◀──on_tool_start()─│                │              │
  │                      │                   │──call()────────────────────▶│
  │                      │                   │◀──result───────────────────│
  │◀─tool_end────────────│◀──on_tool_end()───│                │              │
  │                      │                   │  (loop or done)│              │
  │◀─done────────────────│◀──return──────────│                │              │
```

### Authentication flow

```
Browser                    Server
  │                          │
  │──GET /__auth_status─────▶│  Check if password is set
  │◀─{needs_setup: true}─────│
  │                          │
  │──POST /api/setup─────────▶│  Set password (first time)
  │◀─{token: "jwt..."}───────│
  │                          │
  │──WebSocket /ws?token=jwt─▶│  All subsequent communication
```
