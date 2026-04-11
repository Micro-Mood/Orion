# Orion

<div align="center">

<h3>🌌 开源 AI 编码智能体 — 自托管的 Copilot</h3>

**一个能读、写、搜索、运行你代码的 AI 助手 — 接入任意大模型**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20macOS-blue.svg)]()
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)

[**English**](README.md)

</div>

---

## ✨ Orion 是什么？

Orion 是一个开源 AI 编码智能体——可以理解为 **GitHub Copilot + Cursor** 的自托管替代，模型自由，运行在浏览器里。

给它一个任务（"重构这个模块"、"加单元测试"、"找出 bug 并修复"），它会自主阅读代码、规划方案、编辑文件、执行命令、循环迭代——就像一个 AI 开发者坐在你旁边。

- **无厂商锁定** — 接入任意 OpenAI 兼容模型（通义千问、DeepSeek、GPT、Claude 等）
- **无需 IDE 插件** — 浏览器打开即用，任何设备
- **完全自主** — AI 不只是"建议代码"，它直接操作：读文件、写代码、跑测试、搜符号

<div align="center">

| 特性 | 说明 |
|------|------|
| 🤖 **自主编码** | AI 在工具循环中自主读取、编辑、搜索、运行代码 |
| 🛠️ **28 个内置工具** | 文件操作、命令执行、代码搜索——通过 [Axon](https://github.com/Micro-Mood/Axon) MCP Server |
| 🔄 **流式响应** | 实时输出，智能 JSON/文本检测 |
| 🧠 **两阶段工具调用** | SELECT → PARAMS → EXEC 循环，比全量注入省 60-80% token |
| 📉 **自动模型降级** | FIFO 模型切换（如 flash → turbo → plus），失败自动降级 |
| 💬 **多会话对话** | 多个对话并行，完整的历史与上下文持久化 |
| 📁 **工作区浏览器** | 内置文件浏览器，实时文件系统监控 |
| 🔐 **认证与安全** | JWT + bcrypt 认证，路径边界限制，危险命令拦截 |
| 🎨 **VS Code 风格 UI** | 暗色 IDE 界面，桌面端和移动端响应式 |
| 🌐 **接入任意模型** | 通义千问、DeepSeek、Kimi、OpenAI、Claude——任意 OpenAI 兼容 API |

</div>

## 📷 截图

<div align="center">

<img src="docs/image/desktop.jpeg" width="800" alt="Orion 桌面端界面">
<p><b>桌面端 — 文件浏览器 + 代码编辑器 + AI 对话</b></p>

<table>
<tr>
<td><img src="docs/image/mobile-chat.jpg" width="260" alt="移动端对话"></td>
<td><img src="docs/image/mobile-editor.jpg" width="260" alt="移动端编辑器"></td>
<td><img src="docs/image/mobile-files.jpg" width="260" alt="移动端文件"></td>
</tr>
<tr>
<td align="center"><b>AI 对话</b></td>
<td align="center"><b>代码查看器</b></td>
<td align="center"><b>文件浏览器</b></td>
</tr>
</table>

</div>

## 🏗️ 架构

```
┌─────────────────────────────────────────┐
│  Web 界面                               │
│  Vue 3 · WebSocket · Markdown · Hljs    │
├─────────────────────────────────────────┤
│  FastAPI 服务端                          │
│  认证 · WebSocket · 静态文件 · 文件监控  │
├─────────────────────────────────────────┤
│  Orion 引擎                             │
│  SELECT → PARAMS → EXEC 工具循环        │
│  流式输出 · 取消 · 上下文 FIFO           │
├──────────────────┬──────────────────────┤
│  LLM 客户端      │  MCP 客户端 (TCP)    │
│  OpenAI 兼容     │  JSON-RPC 2.0       │
│  模型降级        │                      │
└──────────────────┴──────────────────────┤
                   │  Axon MCP Server     │
                   │  (Git 子模块)         │
                   └──────────────────────┘
```

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Git（用于子模块）

### 1. 克隆项目（含子模块）

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
```

如果已经克隆但未拉取子模块：

```bash
git submodule update --init
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
pip install pydantic aiofiles  # Axon 子模块依赖
```

### 3. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，至少设置 LLM API Key：

```json
{
    "llm": {
        "api_key": "sk-your-api-key",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
    }
}
```

也可以使用环境变量：

```bash
export ORION_API_KEY="sk-your-api-key"
export ORION_API_URL="https://api.openai.com/v1"  # 或其他兼容端点
```

### 4. 启动

```bash
cd src
python main.py
```

浏览器打开 `http://127.0.0.1:8080`，首次访问会要求设置登录密码。

## ⚙️ 配置

配置加载优先级：**环境变量 > config.json > 默认值**

### config.json

| 分组 | 字段 | 默认值 | 说明 |
|------|------|--------|------|
| `llm` | `api_key` | `""` | LLM API 密钥 |
| `llm` | `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `llm` | `models` | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | 模型列表（FIFO 降级顺序） |
| `llm` | `temperature` | `0.7` | 采样温度 |
| `llm` | `timeout` | `120` | 请求超时（秒） |
| `axon` | `host` | `127.0.0.1` | Axon MCP Server 地址 |
| `axon` | `port` | `9100` | Axon MCP Server 端口 |
| `axon` | `workspace` | `""` | Axon 工作目录（默认跟随引擎） |
| `engine` | `max_history` | `20` | 最大上下文消息数（FIFO 滑动窗口） |
| `engine` | `max_iterations` | `30` | 每条消息最大工具调用轮次 |
| `engine` | `working_directory` | `""` | 工作目录（默认 `workspace/`） |
| `server` | `host` | `127.0.0.1` | 服务器绑定地址 |
| `server` | `port` | `8080` | 服务器端口 |

### 环境变量

| 变量 | 对应配置 |
|------|----------|
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

## 🛠️ 工具

Orion 通过 [Axon MCP Server](https://github.com/Micro-Mood/Axon) 拥有 **28 个工具**：

### 文件操作（14）
`read_file` · `write_file` · `create_file` · `delete_file` · `copy_file` · `move_file` · `create_directory` · `delete_directory` · `move_directory` · `list_directory` · `stat_path` · `insert_text` · `replace_range` · `delete_range`

### 命令执行（10）
`run_command` · `create_task` · `stop_task` · `del_task` · `task_status` · `list_tasks` · `read_stdout` · `read_stderr` · `write_stdin` · `wait_task`

### 搜索（3）
`find_files` · `search_text` · `find_symbol`

### 系统（1）
`get_system_info`

## 📁 项目结构

```
Orion/
├── config.example.json     # 配置模板
├── requirements.txt        # Python 依赖
├── axon/                   # Axon MCP Server（git 子模块）
├── src/
│   ├── main.py             # 入口——启动 Axon + Uvicorn
│   ├── server.py           # FastAPI + WebSocket 服务端
│   ├── engine.py           # AI 引擎（SELECT → PARAMS → EXEC 循环）
│   ├── llm.py              # LLM 客户端（OpenAI 兼容，模型降级）
│   ├── mcp_client.py       # MCP TCP 客户端（JSON-RPC 2.0）
│   ├── axon_manager.py     # Axon 子进程生命周期管理
│   ├── config.py           # 配置管理（单例模式）
│   ├── context.py          # 对话上下文（FIFO 滑动窗口）
│   ├── prompt.py           # 系统提示词构建
│   ├── store.py            # 会话与消息持久化（JSON 文件）
│   ├── tools.py            # 工具注册表（28 工具 + 控制指令）
│   ├── prompts/
│   │   └── system.md       # 系统提示词模板
│   └── web/                # 前端（Vue 3 SPA）
│       ├── index.html
│       ├── app.js
│       └── style.css
├── data/                   # 运行时数据（自动创建，已 gitignore）
│   ├── sessions.json
│   └── messages/
├── workspace/              # 默认工作目录（已 gitignore）
└── docs/                   # 文档
```

## 🌐 部署

Orion 可以部署在反向代理后进行远程访问。前端自动检测 Base Path，因此可以在任意 URL 前缀下运行（如 `https://example.com/orion/`）。

```bash
# 绑定到所有网络接口
export ORION_HOST="0.0.0.0"
cd src && python main.py
```

生产环境建议使用 Nginx/Caddy 反向代理 + HTTPS + WebSocket 支持。参见 [docs/getting-started.md](docs/getting-started.md#remote-access)。

## 🔒 安全性

- **密码认证** — bcrypt 密码哈希 + JWT token
- **路径边界** — Axon 限制文件操作在工作区范围内
- **危险命令拦截** — Axon 中间件拦截 50+ 种危险命令模式
- **敏感数据隔离** — API 密钥、密码、JWT 密钥存放在 `config.json`（已 gitignore）

## 💡 为什么选 Orion？

| | GitHub Copilot | Cursor | **Orion** |
|---|---|---|---|
| 自托管 | ❌ | ❌ | ✅ |
| 模型自由 | ❌ GPT/Claude | 部分 | ✅ 任意 OpenAI 兼容 |
| 开源 | ❌ | ❌ | ✅ MIT |
| 自主操作（编辑文件） | ✅ | ✅ | ✅ |
| 浏览器使用 | ❌ IDE 插件 | ❌ 桌面应用 | ✅ 任意浏览器 |
| 无需订阅 | ❌ | ❌ | ✅ 只付 API 用量 |

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

[MIT](LICENSE)
