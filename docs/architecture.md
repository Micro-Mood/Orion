# Architecture

## Overview

Orion is a self-hosted AI agent with a browser UI, a FastAPI/WebSocket server, an OpenAI-compatible LLM client, and an Axon MCP Server for tool execution.

```text
┌─────────────────────────────────────────────────────┐
│  Web UI                                             │
│  Vue 3 SPA · WebSocket · Markdown · CodeMirror 6    │
├─────────────────────────────────────────────────────┤
│  FastAPI Server                                     │
│  Auth · WebSocket Hub · Static Files · FS Watch      │
├─────────────────────────────────────────────────────┤
│  Orion Engine                                       │
│  native tool_calls · register_tool · compression     │
├─────────────────┬───────────────────────────────────┤
│  LLM Client     │  MCP Client                        │
│  OpenAI compat  │  JSON-RPC 2.0 over TCP             │
│  model fallback │                                   │
└─────────────────┴───────────────────────────────────┤
                  │  Axon MCP Server                  │
                  │  file · search · command · system │
                  └───────────────────────────────────┘
```

Axon is included under `axon/` via git subtree. Orion can start it as a subprocess or connect to an already-running external Axon server.

## Components

### Web UI (`src/web/`)

Single-page Vue 3 application with a VS Code-style interface.

- Uses Vue 3 from CDN; no build step is required.
- Communicates with the server through one authenticated WebSocket connection.
- Renders assistant messages as ordered `segments`: text, thinking, tool calls, and compression events.
- Provides session list, fork buttons, file browser, CodeMirror editor, settings page, and mobile layout.
- Loads config before showing context-window percentage so token display uses the real configured window.

### FastAPI Server (`src/server.py`)

Serves the static UI, REST auth endpoints, WebSocket API, and workspace file watching.

- First WebSocket message must be `{ "type": "auth", "token": "..." }`.
- Maintains active WebSocket connections and per-session background tasks.
- Persists frontend messages separately from the full AI context.
- Broadcasts streaming deltas, tool events, compression events, token updates, and session updates.
- Uses `watchdog` to debounce filesystem changes and notify the file browser.

### Orion Engine (`src/engine.py`)

The engine owns the LLM loop and the AI context stored in `store.context[]`.

#### Native `tool_calls` + `register_tool`

Orion uses the OpenAI-compatible `tool_calls` protocol directly. The model emits tool calls, Orion executes them through MCP, appends tool results to context, and calls the model again until it returns final text or asks the user a question.

Only meta/control tools are always available:

- `register_tool`
- `unregister_tool`
- `ask`
- `fail`
- `set_session_title`

Axon tools are not all injected as full schemas at startup. The system prompt contains a compact catalog, and the model must first call `register_tool(names=[...])`. Registered tools become callable in subsequent LLM calls, are persisted per session, and are unloaded after `tool_ttl_rounds` idle rounds.

```text
system prompt catalog
  -> register_tool(["read_file"])
  -> next LLM call includes read_file's full schema
  -> tool_call read_file
  -> MCP result returns to LLM
```

#### Context Compression And Memory Archive

Compression is triggered when estimated context usage reaches `compress_at * context_window`.

The engine:

1. Splits context by complete user turns.
2. Protects the active turn.
3. Keeps recent complete turns within budget.
4. Archives older complete turns and existing archive handoff notes.
5. Asks a compression LLM call to produce:
   - `<ORION_ARCHIVE_MD>`: detailed human-readable archive.
   - `<ORION_HANDOFF>`: handoff text for the next LLM call.
6. Writes `.orion/<timestamp>.md` and `.orion/<timestamp>.ctx.json`.
7. Replaces archived context with one `[已压缩历史交接]` system note.
8. Refreshes the system prompt so `.orion/index.json` is visible as a lightweight memory index.

The sidecar contains original `entries`, `covered_msg_ids`, `covered_turn_ids`, archived count, and token estimate. It is used by fork and later compression passes.

### Session Store (`src/store.py`)

JSON file-based persistence.

- `data/sessions.json`: session metadata, token counters, registered tools.
- `data/messages/<sid>.json`: two tracks:
  - `messages[]`: frontend-visible user/assistant messages.
  - `context[]`: full AI context, including assistant tool calls, tool results, system handoff notes, and metadata.
- Uses locks and atomic write-to-temp-then-replace writes.
- Limits message file size and stored message count.

#### Fork

`fork_session()` creates a new session from a target frontend message.

For new data, it uses `metadata.msg_id`, `metadata.turn_id`, `covered_msg_ids`, and archive sidecars to rebuild the correct context prefix. If a memory archive only partially overlaps the fork range, the sidecar entries are recursively inspected so only the allowed prefix is restored. Older data without IDs falls back to timestamp/content heuristics.

### Memory (`src/memory.py`)

Memory archives are normal files under the configured `memory_dir` (default `.orion`) inside the working directory.

```text
.orion/
├── index.json
├── 20260519-151815.md
└── 20260519-151815.ctx.json
```

- Markdown archive: human-readable long-form record.
- JSON sidecar: machine-readable archive boundary and raw entries.
- Index: lightweight list injected into the system prompt.

### LLM Client (`src/llm.py`)

Async client for OpenAI-compatible chat completion APIs.

- FIFO model fallback through `llm.models`.
- Configurable timeout, retries, and temperature.
- Streaming and non-streaming support.
- Usage tracking for prompt, completion, and total tokens.

### MCP Client (`src/mcp_client.py`)

Async TCP JSON-RPC 2.0 client used to call Axon tools.

- Line-delimited JSON messages.
- Connect/ping/call lifecycle.
- Per-call timeout based on configured defaults.

### Axon Manager (`src/axon_manager.py`)

Manages the Axon subprocess when `axon.auto_start` is enabled.

- Starts Axon with the configured host, port, and workspace.
- Detects external Axon instances and avoids taking ownership of them.
- Can restart the managed subprocess from the settings UI.
- Cleans up on server shutdown.

### Tool Registry (`src/tools.py`)

Defines compact tool catalog entries and full OpenAI-compatible schemas.

- 27 Axon tools: file, search, command, system, web.
- 3 control tools: `ask`, `fail`, `set_session_title`.
- 2 meta tools: `register_tool`, `unregister_tool`.

## Data Flow

### User Message

```text
Browser           Server              Engine              LLM API             Axon
  │                 │                   │                    │                  │
  │ send_message    │                   │                    │                  │
  ├────────────────▶│ add user message  │                    │                  │
  │                 ├──────────────────▶│ build context      │                  │
  │                 │                   ├───────────────────▶│ chat             │
  │◀─message_delta──┤◀─on_text──────────┤◀───────────────────┤ stream/text      │
  │                 │                   │◀───────────────────┤ tool_calls       │
  │◀─tool_start─────┤◀─on_tool_start────┤                    │                  │
  │                 │                   ├──────────────────────────────────────▶│ call
  │                 │                   │◀──────────────────────────────────────┤ result
  │◀─tool_end───────┤◀─on_tool_end──────┤                    │                  │
  │                 │                   ├───────────────────▶│ next chat        │
  │◀─message_end────┤◀─result───────────┤◀───────────────────┤ final text       │
  │◀─done───────────┤                   │                    │                  │
```

### Compression Event

```text
context near threshold
  -> compress_start WebSocket event
  -> archive old complete turns to .orion/*.md + .ctx.json
  -> replace old context with handoff system note
  -> compress_end WebSocket event
```

### Authentication

```text
Browser                    Server
  │                          │
  │ GET /__auth_status       │
  │◀ {needs_setup}           │
  │ POST /api/setup|login    │
  │◀ {token}                 │
  │ WebSocket /ws            │
  │── {type:"auth", token} ─▶│
  │◀ {type:"auth_ok"}       │
```