# Orion

<div align="center">

<h3>🌌 一个能动手的 AI，不只会说</h3>

**10 块钱一个月的服务器 + 免费大模型 = 你的私人 AI 助理，随时在线，永不失忆。**

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20macOS-blue.svg)]()
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)

[**English**](README.md)

</div>

---

## Orion 是什么？

ChatGPT、Kimi、Claude——很强，但有个致命问题：**聊完就忘。**

你费劲跟 AI 聊了半天，整理了思路、列了计划……然后关掉窗口，什么都没留下。它不记得你上个月说了什么，更碰不到你的文件。

**Orion 不一样。** 它能读文件、写文件、建文件夹、跑脚本、搜索内容、抓网页——然后把所有成果存成你自己的文件。下次你问它，它翻你的文件就知道了。

> **文件就是记忆。不是什么神秘的"记忆功能"，就是你能打开、能编辑、能搜索的 Markdown 文件。**

## 实际用起来什么感觉？

```
你: "把我刚才说的这段反思存下来"
AI: [创建文件] 已存为 /自我反思记录.md

（两周后）

你: "看看我之前写了什么"
AI: [读取文件] 你写过 3 份反思笔记，最近一篇是关于……
```

```
你: "我在读《非暴力沟通》，帮我记一下今天的领悟"
AI: [写入文件] 已追加到 /书籍/非暴力沟通.md

你: "之前那个关于'意图不等于影响'的领悟在哪？"
AI: [搜索文件] 在你 4 月 11 号的笔记里……
```

```
你: "按主题整理一下我的笔记"
AI: [列目录] 看到 47 个文件
    [逐个读取] 分析内容……
    [创建文件夹] 建了 6 个主题文件夹
    [移动文件] 全部归类完成
AI: "47 篇笔记整理成 6 个分类了。"
```

**它不是在"回答问题"，是在帮你干活。**

## 为什么不直接用 ChatGPT？

| | ChatGPT / Kimi / Claude | **Orion** |
|---|---|---|
| **记忆** | 黑盒"记忆"，谁知道它记了啥 | **你自己的文件**——看得见摸得着 |
| **能干活吗** | 只能*建议*你怎么做 | **直接做**——读写文件、跑命令、自主循环 |
| **数据在哪** | 人家的服务器 | **你自己的机器** |
| **模型** | 锁死一家 | **随便换**——通义千问、DeepSeek、GPT、Claude 都行 |
| **月费** | ChatGPT Plus ¥140/月 | **10 块钱服务器 + 免费模型**（通义千问 Flash 免费额度够日常用） |
| **开源** | ❌ | ✅ MIT，想改就改 |

> 💡 **成本明细**：去 [灵梦云](https://idc.np4.cn/) 买个 10 元/月的轻量服务器，装上 Orion，配上通义千问 Flash（免费），就是你的 24 小时在线 AI 助理。随时随地手机访问。
>
> 一杯奶茶的钱不到。ChatGPT Plus 的钱够你用一年。

## 它能干什么

- **🧠 个人助理** — 记录想法、整理反思、追踪目标。你说，它记，永远在
- **📚 读书笔记** — 边读边聊，把领悟存进文件，随时翻阅
- **📋 清单管理** — TODO、订阅清单、支出记录——说一句话就建好
- **🗂️ 文件整理** — "把笔记按主题归类"——它自己看、自己分、自己搬
- **💻 编程** — 读代码、改代码、跑脚本、调试。它也是一个完整的编程 Agent
- **🌐 信息搜集** — 抓网页内容，帮你整理成文件
- **📊 数据处理** — 分析 CSV/JSON，跑 Python，生成报表

## 截图

<div align="center">

<img src="docs/image/desktop.png" width="800" alt="Orion 桌面端界面">
<p><b>桌面端 — 文件浏览器 + 代码编辑器 + AI 对话</b></p>

<table>
<tr>
<td><img src="docs/image/mobile-chat.png" width="260" alt="移动端对话"></td>
<td><img src="docs/image/mobile-editor.png" width="260" alt="移动端编辑器"></td>
<td><img src="docs/image/mobile-files.png" width="260" alt="移动端文件"></td>
</tr>
<tr>
<td align="center"><b>AI 对话</b></td>
<td align="center"><b>代码编辑器</b></td>
<td align="center"><b>文件浏览器</b></td>
</tr>
</table>

</div>

## 怎么做到的？

Orion 有 27 个工具（读文件、写文件、跑命令、搜索……），AI 会自己决定用哪些：

```
你说一句话
 ↓
AI 选工具 → 填参数 → 执行 → 看结果 → 决定下一步
 ↓
循环，直到搞定
```

这套两阶段工具调用比 OpenAI 的 Function Calling 省 60-80% token。翻译成人话就是：**又快又省钱。**

<details>
<summary><b>架构图（给技术人看的）</b></summary>

```
┌─────────────────────────────────────────┐
│  Web 界面                               │
│  Vue 3 · WebSocket · Markdown · CM6     │
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

</details>

### 特性一览

| | |
|---|---|
| 🧠 **两阶段工具调用** | 比全量 Schema 注入省 60-80% token |
| 📉 **自动模型降级** | 便宜的先上，不行再换贵的 |
| 🔄 **流式响应** | 实时输出，不用干等 |
| 💬 **多会话** | 多个对话并行，历史完整保存 |
| 📁 **文件浏览器** | VS Code 风格，实时监控文件变化 |
| ✏️ **代码编辑器** | CodeMirror 6，13+ 语言高亮 |
| 💭 **思考过程** | 能看到 AI 在想什么（支持 thinking 的模型） |
| 🔐 **认证** | JWT + bcrypt，部署到公网也安全 |
| 🎨 **暗色主题** | 程序员看着舒服的那种 |

## 快速开始

### 需要什么

- Python 3.10+
- Git

### 1. 克隆

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
```

子模块没拉到的话：

```bash
git submodule update --init
```

### 2. 装依赖

```bash
pip install -r requirements.txt
pip install -r axon/requirements.txt
```

### 3. 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`，填入你的 API Key：

```json
{
    "llm": {
        "api_key": "sk-your-api-key",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-flash", "qwen-turbo", "qwen-plus"]
    }
}
```

> 通义千问 API Key 在[百炼平台](https://bailian.console.aliyun.com/)免费申请。Flash 模型有充足的免费额度。

也可以用环境变量：

```bash
export ORION_API_KEY="sk-your-api-key"
```

### 4. 启动

```bash
cd src
python main.py
```

打开 `http://127.0.0.1:8080`，设个密码，开聊。

## 部署到服务器（10 元/月方案）

想随时随地用？买个便宜的云服务器，比如 [灵梦云](https://idc.np4.cn/) 10 元/月的轻量机，然后：

```bash
# 服务器上
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
pip install -r requirements.txt
pip install -r axon/requirements.txt
cp config.example.json config.json
# 编辑 config.json，填 API Key

# 绑定所有网络接口
export ORION_HOST="0.0.0.0"
cd src && python main.py
```

配个 Nginx 反向代理 + HTTPS，就能在手机上随时访问你的 AI。

> 前端自动检测 Base Path，所以你可以把它放在 `https://你的域名/orion/` 下面，和其他服务共存。

详细部署教程见 [docs/getting-started.md](docs/getting-started.md#remote-access)。

## 配置参考

配置优先级：**环境变量 > config.json > 默认值**

<details>
<summary><b>config.json 字段</b></summary>

| 分组 | 字段 | 默认值 | 说明 |
|------|------|--------|------|
| `llm` | `api_key` | `""` | LLM API 密钥 |
| `llm` | `base_url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容接口地址 |
| `llm` | `models` | `["qwen-flash", "qwen-turbo", "qwen-plus"]` | 模型列表（优先用便宜的） |
| `llm` | `temperature` | `0.7` | 采样温度 |
| `llm` | `timeout` | `120` | 请求超时（秒） |
| `axon` | `host` | `127.0.0.1` | Axon MCP Server 地址 |
| `axon` | `port` | `9100` | Axon MCP Server 端口 |
| `axon` | `workspace` | `""` | 工作目录 |
| `engine` | `max_history` | `20` | 上下文消息数 |
| `engine` | `max_iterations` | `30` | 每条消息最大工具调用轮次 |
| `engine` | `read_file_max_lines` | `200` | 默认读取行数上限 |
| `engine` | `working_directory` | `""` | 工作目录（默认 `workspace/`） |
| `server` | `host` | `127.0.0.1` | 绑定地址 |
| `server` | `port` | `8080` | 端口 |

</details>

<details>
<summary><b>环境变量</b></summary>

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

</details>

## 27 个内置工具

通过 [Axon MCP Server](https://github.com/Micro-Mood/Axon) 提供：

| 分类 | 工具 |
|------|------|
| **文件**（12） | `read_file` · `write_file` · `delete_file` · `copy_file` · `move_file` · `create_directory` · `delete_directory` · `move_directory` · `list_directory` · `stat_path` · `replace_string_in_file` · `multi_replace_string_in_file` |
| **命令**（10） | `run_command` · `create_task` · `stop_task` · `del_task` · `task_status` · `list_tasks` · `read_stdout` · `read_stderr` · `write_stdin` · `wait_task` |
| **搜索**（3） | `find_files` · `search_text` · `find_symbol` |
| **系统**（1） | `get_system_info` |
| **网络**（1） | `fetch_webpage` |

## 项目结构

```
Orion/
├── config.example.json     # 配置模板
├── requirements.txt        # Python 依赖
├── axon/                   # Axon MCP Server（git 子模块）
├── src/
│   ├── main.py             # 入口
│   ├── server.py           # FastAPI + WebSocket
│   ├── engine.py           # AI 引擎（工具循环）
│   ├── llm.py              # LLM 客户端（模型降级）
│   ├── mcp_client.py       # MCP TCP 客户端
│   ├── axon_manager.py     # Axon 子进程管理
│   ├── config.py           # 配置
│   ├── context.py          # 对话上下文
│   ├── prompt.py           # 系统提示词
│   ├── store.py            # 会话持久化
│   ├── tools.py            # 工具注册表
│   ├── prompts/
│   │   └── system.md       # 系统提示词模板
│   └── web/                # 前端
├── data/                   # 运行时数据（gitignore）
├── workspace/              # 默认工作目录（gitignore）
└── docs/
```

## 安全性

- **密码认证** — bcrypt + JWT
- **路径沙箱** — 文件操作限制在工作区内
- **危险命令拦截** — 50+ 种危险命令模式自动拦截
- **敏感数据隔离** — 密钥存在 `config.json`（已 gitignore）

## 贡献

欢迎 Issue 和 PR！

## 许可证

[MIT](LICENSE) — 想怎么用就怎么用。
