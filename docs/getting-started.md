# Getting Started

## Prerequisites

- **Python 3.10+**
- **Git** (for cloning with submodule)
- An API key from any OpenAI-compatible LLM provider:
  - [Alibaba DashScope](https://dashscope.aliyuncs.com/) (Qwen series)
  - [DeepSeek](https://platform.deepseek.com/)
  - [Moonshot/Kimi](https://platform.moonshot.cn/)
  - [OpenAI](https://platform.openai.com/)

## Installation

### 1. Clone the repository

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
```

The `--recurse-submodules` flag automatically pulls the [Axon](https://github.com/Micro-Mood/Axon) MCP Server into the `axon/` directory.

If you already cloned without it:

```bash
git submodule update --init
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Dependencies:
- `fastapi` — Web framework
- `uvicorn[standard]` — ASGI server
- `httpx` — Async HTTP client (for LLM API calls)
- `pyjwt` — JWT token generation/verification
- `bcrypt` — Password hashing
- `watchdog` — Filesystem monitoring (real-time file explorer updates)

> Axon has its own dependencies (`pydantic`, `aiofiles`, `aiohttp`). When launched as a subprocess by Orion, ensure they are available in the same Python environment:
> ```bash
> pip install -r axon/requirements.txt
> ```

### 3. Configure

```bash
cp config.example.json config.json
```

Edit `config.json` with your API key:

```json
{
    "llm": {
        "api_key": "sk-your-api-key-here",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    }
}
```

See [Configuration](configuration.md) for all options.

### 4. Run

```bash
cd src
python main.py
```

You should see:

```
Orion 启动中...
  正在拉起 Axon MCP Server...
  Axon: 127.0.0.1:9100 (PID=xxxxx)
  地址: http://127.0.0.1:8080
  模型: qwen-flash, qwen-turbo, qwen-plus
  API Key: 已配置
  工作目录: .../Orion/workspace
```

### 5. Open in browser

Navigate to `http://127.0.0.1:8080`. On first visit you'll be prompted to set a login password (minimum 6 characters).

## Using a different LLM provider

Orion works with any provider that exposes an OpenAI-compatible `/v1/chat/completions` endpoint.

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

## Remote access

By default, Orion binds to `127.0.0.1` (localhost only). To access from other devices:

```json
{
    "server": {
        "host": "0.0.0.0",
        "port": 8080
    }
}
```

> **Warning**: Exposing Orion to the network means anyone who can reach the port can attempt to log in. Ensure you set a strong password. For production deployments, place Orion behind a reverse proxy (Nginx, Caddy) with HTTPS.

## Troubleshooting

### Axon fails to start

```
[!] Axon 启动失败，工具调用将不可用
```

- Ensure `axon/src/__main__.py` exists. If not: `git submodule update --init`
- Ensure Axon's dependencies are installed: `pip install pydantic aiofiles`
- Check if port 9100 is already in use

### API Key not configured

You can configure the API key in three ways:
1. Edit `config.json` directly
2. Set `ORION_API_KEY` environment variable
3. Use the Settings page in the web UI (gear icon, bottom-left)

### WebSocket disconnects

- Check if uvicorn is running (terminal output)
- Ensure no firewall is blocking WebSocket connections
- Try refreshing the page — the client auto-reconnects
