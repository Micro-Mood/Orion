# Getting Started

This guide walks through a full Orion setup: local installation, first login, LLM configuration, Axon tool backend setup, remote access, updates, backups, and common troubleshooting.

## What You Are Installing

Orion has three runtime parts:

- The FastAPI server in `src/`, which serves the web UI, REST auth endpoints, and WebSocket API.
- The browser UI in `src/web/`, a Vue 3 single-page app served directly by FastAPI.
- Axon under `axon/`, the MCP tool execution backend used for file operations, search, commands, system info, and web fetches.

Axon is included in the repository under `axon/` via git subtree. A normal `git clone` is enough; no extra Git setup is needed.

## Prerequisites

- Python 3.10 or newer.
- Git.
- A terminal with access to the project directory.
- An API key from an OpenAI-compatible chat completions provider.

Supported provider examples:

- [Alibaba DashScope](https://dashscope.aliyuncs.com/) for Qwen models.
- [DeepSeek](https://platform.deepseek.com/).
- [Moonshot/Kimi](https://platform.moonshot.cn/).
- [OpenAI](https://platform.openai.com/).

Orion calls the provider through an OpenAI-compatible `/v1/chat/completions` API. Model support depends on whether the provider implements the expected chat completion and tool-call behavior.

## 1. Clone The Repository

```bash
git clone https://github.com/Micro-Mood/Orion.git
cd Orion
```

Verify that Axon exists:

```bash
test -f axon/src/__main__.py && echo "Axon is present"
```

On Windows PowerShell:

```powershell
Test-Path axon/src/__main__.py
```

Expected result: the file exists. If `axon/` is missing, the checkout is incomplete; clone the repository again or inspect the downloaded archive.

## 2. Create A Python Environment

Using a virtual environment is recommended so Orion and Axon dependencies do not leak into your global Python installation.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### Windows PowerShell

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

If PowerShell blocks script execution, run this once for the current shell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Install Dependencies

Install Orion dependencies:

```bash
pip install -r requirements.txt
```

Install Axon dependencies in the same environment:

```bash
pip install -r axon/requirements.txt
```

Orion starts Axon as a subprocess by default, so both dependency sets must be available to the Python interpreter that runs `src/main.py`.

Key Orion dependencies:

- `fastapi` for HTTP and WebSocket endpoints.
- `uvicorn[standard]` for the ASGI server.
- `httpx` for LLM API calls.
- `pyjwt` for login tokens.
- `bcrypt` for password hashing.
- `watchdog` for real-time file browser refresh.

Key Axon dependencies include `pydantic`, `aiofiles`, and `aiohttp`.

## 4. Create Configuration

Copy the example file:

```bash
cp config.example.json config.json
```

On Windows PowerShell:

```powershell
Copy-Item config.example.json config.json
```

At minimum, set `llm.api_key`. The default provider endpoint is Alibaba DashScope compatible mode:

```json
{
  "llm": {
    "api_key": "sk-your-api-key-here",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
  }
}
```

`config.json` is ignored by git. It may contain secrets such as API keys, password hashes, and JWT secrets.

See [Configuration](configuration.md) for the complete field reference.

## 5. Understand The Important Settings

### LLM Settings

- `llm.api_key`: provider API key.
- `llm.base_url`: OpenAI-compatible API base URL.
- `llm.models`: fallback list. Orion tries models in order.
- `llm.temperature`: sampling temperature.
- `llm.timeout`: request timeout in seconds.
- `llm.max_retries`: retry attempts per model.

### Axon Settings

- `axon.host` and `axon.port`: TCP address for Axon MCP Server.
- `axon.auto_start`: when enabled, Orion starts Axon as a subprocess.
- `axon.workspace`: optional workspace path for Axon. Empty means use Orion's effective working directory.

### Engine Settings

- `engine.working_directory`: main workspace for file operations. Empty falls back to `axon.workspace`, then `workspace/`.
- `engine.tool_ttl_rounds`: unregister idle tool schemas after N rounds.
- `engine.context_window`: model context window estimate.
- `engine.compress_at`: compression threshold ratio. `0` disables compression.
- `engine.context_recent_n`: number of recent complete turns to keep outside archives.
- `engine.memory_dir`: archive directory relative to the effective working directory, default `.orion`.

## 6. Provider Examples

### Alibaba DashScope / Qwen

```json
{
  "llm": {
    "api_key": "sk-your-qwen-key",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
  }
}
```

### DeepSeek

```json
{
  "llm": {
    "api_key": "sk-your-deepseek-key",
    "base_url": "https://api.deepseek.com/v1",
    "models": ["deepseek-chat"]
  }
}
```

### OpenAI

```json
{
  "llm": {
    "api_key": "sk-your-openai-key",
    "base_url": "https://api.openai.com/v1",
    "models": ["gpt-4o-mini", "gpt-4o"]
  }
}
```

### Moonshot / Kimi

```json
{
  "llm": {
    "api_key": "sk-your-kimi-key",
    "base_url": "https://api.moonshot.cn/v1",
    "models": ["moonshot-v1-8k"]
  }
}
```

## 7. Environment Variable Overrides

Configuration priority is:

```text
Environment variables > config.json > defaults
```

Common examples:

```bash
export ORION_API_KEY="sk-your-api-key"
export ORION_PORT=3000
export ORION_WORKING_DIR="/home/user/orion-workspace"
```

Windows PowerShell:

```powershell
$env:ORION_API_KEY = "sk-your-api-key"
$env:ORION_PORT = "3000"
$env:ORION_WORKING_DIR = "C:\Users\you\orion-workspace"
```

Use [Configuration](configuration.md#environment-variables) for the full environment variable list.

## 8. Start Orion

From the repository root:

```bash
cd src
python main.py
```

You should see output like:

```text
Orion 启动中...
  正在拉起 Axon MCP Server...
  Axon: 127.0.0.1:9100 (PID=12345)
  地址: http://127.0.0.1:8080
  模型: qwen-flash, qwen-turbo, qwen-plus
  API Key: 已配置
  工作目录: .../Orion/workspace
```

The important parts are the Axon status, URL, configured model list, API key status, and working directory.

If port `9100` is already used by another Axon process, Orion will treat it as an external Axon server and will not stop or restart it.

## 9. First Login

Open:

```text
http://127.0.0.1:8080
```

On first visit, Orion asks you to set a login password. The password must be at least 6 characters. Orion stores a bcrypt hash in `config.json`; it does not store the plaintext password.

After setup, the browser receives a JWT token and authenticates the WebSocket by sending the token as the first WebSocket message.

## 10. First Smoke Test

After login:

1. Open Settings.
2. Click the LLM test button. It should report that the model connection works.
3. Click the Axon test button. It should report that Axon is reachable.
4. Open the file browser and load the workspace root.
5. Send a small message such as: `Create a short note named hello-orion.md in the workspace.`
6. Confirm dangerous tools if prompted.
7. Verify the created file in the file browser.

This checks the full path: browser -> WebSocket -> Orion engine -> LLM -> Axon tool call -> filesystem -> browser refresh.

## 11. Working Directory And Memory Files

Orion resolves the effective working directory in this order:

1. `engine.working_directory`
2. `axon.workspace`
3. `Orion/workspace/`

Runtime data is stored in several places:

| Path | Purpose |
|------|---------|
| `config.json` | Local configuration, secrets, auth data. |
| `data/sessions.json` | Session metadata and token counters. |
| `data/messages/*.json` | Frontend messages and full AI context. |
| `workspace/` | Default workspace when no custom directory is configured. |
| `<working_directory>/.orion/` | Long-term memory archives and archive sidecars. |

Back up `config.json`, `data/`, your working directory, and `.orion/` if you want to preserve all conversation and memory state.

## 12. Remote Access

By default, Orion binds to `127.0.0.1`, which only accepts local connections. To access it from other devices, bind to all interfaces:

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8080
  }
}
```

or use environment variables:

```bash
export ORION_HOST="0.0.0.0"
export ORION_PORT=8080
```

Important security notes:

- Set a strong password before exposing Orion to a network.
- Use HTTPS when exposing Orion over the internet.
- Put Orion behind Nginx, Caddy, or another reverse proxy for public deployments.
- Keep `config.json`, `data/`, and `.orion/` private.
- Be careful with `engine.auto_confirm_dangerous`; the safer default is `false`.

### Nginx Reverse Proxy Example

```nginx
server {
    listen 443 ssl http2;
    server_name example.com;

    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;

    location /orion/ {
        proxy_pass http://127.0.0.1:8080/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

The frontend detects its base path, so deployments under prefixes such as `/orion/` are supported.

### systemd Service Example

Create `/etc/systemd/system/orion.service`:

```ini
[Unit]
Description=Orion AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/Orion/src
Environment=ORION_HOST=127.0.0.1
Environment=ORION_PORT=8080
ExecStart=/opt/Orion/.venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now orion
sudo systemctl status orion
```

## 13. Updating Orion

From the repository root:

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
pip install -r axon/requirements.txt
```

Windows PowerShell:

```powershell
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r axon/requirements.txt
```

Then restart Orion. If you use systemd:

```bash
sudo systemctl restart orion
```

Because Axon is included under `axon/`, `git pull` updates the tracked Axon files that belong to the Orion repository state.

## 14. Troubleshooting

### Axon Fails To Start

Terminal output:

```text
[!] Axon 启动失败，工具调用将不可用
```

Check:

- `axon/src/__main__.py` exists.
- Axon dependencies are installed: `pip install -r axon/requirements.txt`.
- Port `9100` is not blocked or occupied by an unrelated process.
- The configured workspace path exists and is writable.

Useful checks:

```bash
python -m pip show aiofiles pydantic aiohttp
python -m src --help
```

Run the second command from inside the `axon/` directory if you need to test Axon manually.

### API Key Is Not Configured

Symptoms:

- Startup output says the API key is not configured.
- The LLM test fails in Settings.
- Chat requests return an AI service error.

Fix it in one of three ways:

1. Edit `config.json` and set `llm.api_key`.
2. Set `ORION_API_KEY` before starting Orion.
3. Use the Settings page in the web UI.

### Provider Endpoint Or Model Fails

Check:

- `llm.base_url` includes the provider API base, usually ending in `/v1` or compatible-mode `/v1`.
- `llm.models` contains model names accepted by that provider.
- The provider account has quota.
- The selected model supports tool calls if you expect file or command operations.

### WebSocket Disconnects

Check:

- The browser can load the main page.
- The reverse proxy includes `Upgrade` and `Connection` headers.
- Proxy read timeout is long enough for streaming responses.
- Firewall rules allow the configured HTTP port.
- If running behind a prefix such as `/orion/`, the proxy preserves WebSocket requests to `/ws` under that prefix.

The client auto-reconnects after short disconnects.

### Port Already In Use

If the HTTP port is used, change `server.port` or `ORION_PORT`.

If the Axon port is used by an existing Axon server, Orion will connect to it as external. If the process on that port is not Axon, stop it or change `axon.port`.

### Password Reset

Stop Orion, open `config.json`, and clear `auth.password_hash`:

```json
{
  "auth": {
    "password_hash": "",
    "jwt_secret": "keep-existing-secret-if-present",
    "token_expiry_hours": 72
  }
}
```

Start Orion again and open the web UI. It will show the first-run setup page. Keep the existing `jwt_secret` unless you intentionally want to invalidate all existing tokens.

### File Browser Does Not Update

Check:

- `watchdog` is installed.
- The effective working directory exists.
- Orion has permission to read the directory.
- For remote mounts or network filesystems, manual refresh may be more reliable than filesystem events.

### Dangerous Tool Confirmation Appears

This is expected for write/delete/command operations. Confirm only actions you understand. The setting `engine.auto_confirm_dangerous` can skip these prompts, but it is not recommended for internet-exposed deployments.

## Next Steps

- Read [Architecture](architecture.md) for the tool registration, compression, memory archive, and fork design.
- Read [Configuration](configuration.md) for all config fields and environment variables.
- Read [WebSocket Protocol](websocket-protocol.md) if you want to integrate another frontend or automation client.