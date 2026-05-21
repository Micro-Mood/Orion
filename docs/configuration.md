# Configuration

Orion uses layered configuration:

```text
Environment variables > config.json > defaults
```

`config.json` lives at the project root. It is ignored by git because it may contain API keys, password hashes, and JWT secrets.

```bash
cp config.example.json config.json
```

## Full Example

```json
{
  "llm": {
    "api_key": "sk-your-api-key",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "models": ["qwen-flash", "qwen-turbo", "qwen-plus"],
    "temperature": 0.7,
    "timeout": 120,
    "max_retries": 3
  },
  "axon": {
    "host": "127.0.0.1",
    "port": 9100,
    "connect_timeout": 5.0,
    "call_timeout": 60.0,
    "auto_start": true,
    "workspace": ""
  },
  "engine": {
    "max_iterations": 30,
    "working_directory": "",
    "stream_chunk_size": 4,
    "stream_chunk_delay": 0.02,
    "read_file_max_lines": 200,
    "auto_confirm_dangerous": false,
    "tool_ttl_seconds": 300,
    "context_window": 128000,
    "compress_at": 0.55,
    "context_recent_n": 4,
    "memory_dir": ".orion"
  },
  "server": {
    "host": "127.0.0.1",
    "port": 8080
  },
  "auth": {
    "token_expiry_hours": 72
  }
}
```

On first startup, Orion auto-generates `auth.jwt_secret` and saves it to `config.json`. The login password hash is created through the web UI first-run setup.

## Section Reference

### `llm`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_key` | string | `""` | API key for the OpenAI-compatible provider. Can also be set in the web UI. |
| `base_url` | string | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Chat completions endpoint base URL. |
| `models` | string[] | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | FIFO fallback list. The client tries models in order. |
| `temperature` | float | `0.7` | Sampling temperature. |
| `timeout` | int | `120` | LLM HTTP timeout in seconds. |
| `max_retries` | int | `3` | Retry attempts per model before falling back. |

### `axon`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `127.0.0.1` | Axon MCP Server host. |
| `port` | int | `9100` | Axon MCP Server port. |
| `connect_timeout` | float | `5.0` | TCP connection timeout in seconds. |
| `call_timeout` | float | `60.0` | Default tool call timeout in seconds. |
| `auto_start` | bool | `true` | Whether Orion starts Axon as a subprocess. |
| `workspace` | string | `""` | Axon workspace. Empty means use the effective Orion working directory. |

Axon is included under `axon/` via git subtree. Install `axon/requirements.txt` in the same Python environment because Orion starts Axon as a subprocess when `auto_start` is enabled.

### `engine`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_iterations` | int | `30` | Max LLM/tool loop iterations for one user message. `0` means unlimited. |
| `working_directory` | string | `""` | Primary workspace. Empty falls back to `axon.workspace`, then `Orion/workspace/`. |
| `stream_chunk_size` | int | `4` | Characters per server-to-client text chunk. |
| `stream_chunk_delay` | float | `0.02` | Delay between text chunks in seconds. |
| `read_file_max_lines` | int | `200` | Default line budget used by file-reading behavior. |
| `auto_confirm_dangerous` | bool | `false` | Skip confirmation for dangerous tools. Use with care. |
| `tool_ttl_seconds` | int | `300` | Unregister tools after N idle seconds. `0` disables TTL. |
| `context_window` | int | `128000` | Model context window used for compression threshold estimates. |
| `compress_at` | float | `0.55` | Trigger compression when estimated context usage reaches this ratio. Lower values reduce uncached input cost; `0` disables compression. |
| `context_recent_n` | int | `4` | Max recent complete turns kept outside archive during compression. |
| `memory_dir` | string | `.orion` | Memory archive directory relative to the effective working directory. |

### `server`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `127.0.0.1` | HTTP bind address. Use `0.0.0.0` for external access. |
| `port` | int | `8080` | HTTP/WebSocket port. |

### `auth`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `password_hash` | string | `""` | bcrypt password hash, managed by first-run setup. |
| `jwt_secret` | string | auto-generated | JWT signing key, generated and saved on first startup. |
| `token_expiry_hours` | int | `72` | JWT token validity in hours. |

## Environment Variables

Environment variables override `config.json` values:

| Variable | Type | Maps to |
|----------|------|---------|
| `ORION_API_KEY` | string | `llm.api_key` |
| `ORION_API_URL` | string | `llm.base_url` |
| `ORION_TEMPERATURE` | float | `llm.temperature` |
| `ORION_AXON_HOST` | string | `axon.host` |
| `ORION_AXON_PORT` | int | `axon.port` |
| `ORION_AXON_WORKSPACE` | string | `axon.workspace` |
| `ORION_MAX_ITERATIONS` | int | `engine.max_iterations` |
| `ORION_WORKING_DIR` | string | `engine.working_directory` |
| `ORION_TOOL_TTL_SECONDS` | int | `engine.tool_ttl_seconds` |
| `ORION_CONTEXT_WINDOW` | int | `engine.context_window` |
| `ORION_COMPRESS_AT` | float | `engine.compress_at` |
| `ORION_CONTEXT_RECENT_N` | int | `engine.context_recent_n` |
| `ORION_HOST` | string | `server.host` |
| `ORION_PORT` | int | `server.port` |

Example:

```bash
export ORION_API_KEY="sk-your-key"
export ORION_PORT=3000
cd src && python main.py
```

## Effective Working Directory

The effective working directory is resolved in this order:

1. `engine.working_directory`
2. `axon.workspace`
3. `Orion/workspace/`

It is exposed to the frontend as `effective_cwd` in the `config_data` WebSocket message.

## Web UI Settings

The settings panel can update LLM, Axon, and engine options at runtime. `save_config` writes the updated `config.json`, reconfigures the LLM client, MCP client, engine settings, Axon manager, and file watcher as needed. API keys returned to the frontend are masked unless saved directly by the user.