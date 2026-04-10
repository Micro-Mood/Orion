# Configuration

Orion uses a layered configuration system with three priority levels:

```
Environment Variables  >  config.json  >  Defaults
```

## config.json

Located at the project root (`Orion/config.json`). Copy from the template to get started:

```bash
cp config.example.json config.json
```

> **Note**: `config.json` is gitignored and should never be committed — it may contain API keys and password hashes.

### Full example

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
        "max_history": 20,
        "max_iterations": 30,
        "working_directory": "",
        "stream_chunk_size": 4,
        "stream_chunk_delay": 0.02
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8080
    }
}
```

### Section reference

#### `llm` — LLM Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_key` | string | `""` | API key for the LLM provider. Can also be set via web UI settings. |
| `base_url` | string | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI-compatible API endpoint. |
| `models` | string[] | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | Model list in FIFO fallback order. First model is tried first; on failure, falls back to the next. |
| `temperature` | float | `0.7` | Sampling temperature (0.0–2.0). |
| `timeout` | int | `120` | HTTP request timeout in seconds. |
| `max_retries` | int | `3` | Max retry attempts per model before falling back. |

#### `axon` — Axon MCP Server Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `127.0.0.1` | Axon server bind address. |
| `port` | int | `9100` | Axon server port. |
| `connect_timeout` | float | `5.0` | TCP connection timeout in seconds. |
| `call_timeout` | float | `60.0` | Default tool call timeout in seconds. |
| `auto_start` | bool | `true` | Whether Orion should auto-start Axon as a subprocess. |
| `workspace` | string | `""` | Working directory for Axon. Empty = use engine's working directory. |

#### `engine` — Engine Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_history` | int | `20` | Maximum messages in the FIFO context window. Older messages are dropped. |
| `max_iterations` | int | `30` | Maximum SELECT→PARAMS→EXEC iterations per user message. Prevents infinite tool loops. |
| `working_directory` | string | `""` | AI's working directory. Empty = `Orion/workspace/`. |
| `stream_chunk_size` | int | `4` | Number of characters per streaming chunk sent to the client. |
| `stream_chunk_delay` | float | `0.02` | Delay between streaming chunks (seconds). |

#### `server` — Web Server Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `127.0.0.1` | Bind address. Use `0.0.0.0` for external access. |
| `port` | int | `8080` | HTTP/WebSocket port. |

#### `auth` — Authentication (auto-managed)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `password_hash` | string | `""` | bcrypt hash of the login password. Set via web UI on first visit. |
| `jwt_secret` | string | `""` | JWT signing key. Auto-generated on first startup. |
| `token_expiry_hours` | int | `72` | JWT token validity period in hours. |

> The `auth` section is managed automatically. You don't need to set it manually.

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
| `ORION_MAX_HISTORY` | int | `engine.max_history` |
| `ORION_MAX_ITERATIONS` | int | `engine.max_iterations` |
| `ORION_WORKING_DIR` | string | `engine.working_directory` |
| `ORION_HOST` | string | `server.host` |
| `ORION_PORT` | int | `server.port` |

Example:

```bash
export ORION_API_KEY="sk-your-key"
export ORION_PORT=3000
cd src && python main.py
```

## Web UI Settings

Most configuration can be changed at runtime through the web UI settings panel (gear icon in the bottom-left). Changes are saved to `config.json` and take effect immediately without restart.
