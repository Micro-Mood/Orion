# Orion

<div align="center">

<h3>自托管 AI 助手 — 文件即记忆，工具按需加载</h3>

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20|%20Linux%20|%20macOS-blue.svg)]()
[![Vue](https://img.shields.io/badge/Vue-3-42b883.svg)](https://vuejs.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)

[**English**](README.md)

</div>

---

## 为什么需要 Orion？

现有的 AI Agent 框架有一个共通的浪费问题。

**传统 Function Calling** 把所有工具的完整定义——名称、描述、每个参数名、类型、参数描述——全量注入 system prompt。27 个工具的完整 JSON Schema 轻松吃掉几万 token。但绝大多数对话只用其中 3-5 个工具，剩下的全是无效负载。

**长对话更糟**：消息越堆越多，几万 token 的工具 Schema 还压在 system prompt 里。要么截断丢上下文，要么 token 账单爆炸。

Orion 针对这些问题给出了设计层面的解决方案。

---

## 创新设计

### 1. register_tool：按需注册，而非全量注入

**传统 Function Calling** 在 system prompt 里注入的是这样的内容——每个工具都是完整 JSON Schema：

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "读取指定路径的文件内容并返回文本字符串",
    "parameters": {
      "type": "object",
      "properties": {
        "file_path": { "type": "string", "description": "需要读取的文件的完整路径" },
        "encoding": { "type": "string", "description": "文件编码格式，默认为 utf-8", "enum": ["utf-8", "gbk", "latin-1"], "default": "utf-8" },
        "max_chars": { "type": "integer", "description": "最多读取的字符数", "minimum": 1 }
      },
      "required": ["file_path"]
    }
  }
}
```

27 个工具每个都有类似的完整 Schema，**始终压在 system prompt 里**。

**Orion 的做法**：system prompt 只放名称和一行简介：

```
### file
- `read_file`: Read file content
- `write_file`: Write file (create if not exists) [dangerous]
```

需要时才注册，注册后才注入完整参数：

```
LLM 需要读文件
  → register_tool(names=["read_file"])      ← 只有这一小段 payload
  → 引擎将 read_file 的完整 Schema 注入 tool_choice
  → 下一轮即可调用 read_file(path="/xxx")    ← 后续零额外 overhead
  闲置 N 轮后 TTL 自动卸载，不占上下文
```

**关键机制**：
- **注册即许可**：不注册的工具 LLM 无法调用
- **下一轮才生效**：避免 LLM 在同一轮里盲目调用不熟悉的工具
- **TTL 自动卸载**：注册后连续闲置超过阈值自动移除，防止上下文膨胀
- **跨会话持久化**：用户断开重连，已注册列表自动从 session store 恢复

**对比总结**：

| | 传统 Function Calling | Orion register_tool |
|---|---|---|
| System prompt 工具描述 | 完整 JSON Schema（名称、描述、参数名、类型、参数描述） | 名称 + 一行简介 |
| 完整参数 Schema 何时出现 | 始终在 system prompt 里 | 调 register_tool 后才注入 tool_choice |
| 工具生命周期 | 永远在场 | TTL 闲置自动卸载 |
| 跨会话 | 每次重连重新注入全部 | 持久化到 session store |

### 2. 压缩记忆：上下文超限自动归档

**问题**：长对话上下文爆炸后，常见做法是滑动窗口截断——直接丢掉最早的消息。关键决策、用户偏好、未完成的任务跟着一起丢。

**Orion 的做法**：上下文超限时不截断，触发一次独立的 LLM 调用，把旧对话压缩成两个产物：

```
旧对话（30 条消息，8000 token）
        │
        ▼  压缩 LLM 调用
        │
   ┌────┴────┐
   ▼         ▼
详细归档    接续交接
(MD文件)   (System Note)

写入 .orion/xxx.md       注入上下文替代旧消息
人类可以直接打开看          LLM 靠它继续对话
```

每次压缩的产物：
- **详细归档** → 写入 `.orion/<时间戳>.md`，包含完整对话流、关键决策、用户原话
- **接续交接** → 注入 system note，包含当前状态、未完成任务、约束

**压缩范围选择**：引擎会智能判断哪些消息可以归档——只归档已经完成的对话轮次，当前正在进行的消息被保护，不会被压缩。

**索引懒加载**：归档后 system prompt 只放索引：

```
## 长期记忆索引
- xxx讨论 — .orion/20260519-151815.md
- yyy决策 — .orion/20260519-132359.md
如与当前任务相关，主动 register_tool 后 read_file 加载完整内容。
```

**和向量数据库的对比**：

| | 向量数据库 | Orion |
|---|---|---|
| 存储格式 | 向量嵌入，不可读 | Markdown 文件，`cat` 就能看 |
| 检索 | 语义相似度搜索，结果黑盒 | LLM 自主判断 + 完整文件加载 |
| 归档粒度 | 碎片化 chunk | 完整对话流 + 接续交接 |
| 增量可追溯 | 无法追踪覆盖了哪些消息 | `.ctx.json` 记录 `covered_msg_ids` |

### 3. 文件即记忆 + Fork 体系

所有记忆都是文件系统里的普通文件，不是黑盒向量数据库。

```
.orion/
├── index.json                   ← 索引（标题 + 路径 + 时间）
├── 20260519-151815.md           ← 人类可读的详细归档
└── 20260519-151815.ctx.json     ← 机器侧边数据（记录覆盖了哪些消息）
```

**Fork 会话**：可以从任意一条消息处切分创建新会话，归档的记忆通过 `covered_msg_ids` 精确判断是否属于新会话的范围。旧会话和新会话的归档文件是独立的，互不干扰。

下次对话启动时，system prompt 自动加载最新索引。AI 不需要你提醒「我们之前聊过什么」。

### 4. 原生 tool_calls 循环

Orion 使用 OpenAI 原生的 `tool_calls` 协议，不引入自定义 JSON 解析。

```
用户输入
  → LLM 流式输出（文本实时推送，支持 thinking 过程）
  → 如果有 tool_calls：引擎直接执行，结果返回给 LLM
  → 继续循环，直到 LLM 输出纯文本为止
  → 纯文本输出即代表本轮结束
```

- **流式输出**：文本实时推送，不用等到全部生成完
- **危险工具确认**：写文件、删文件、跑命令等操作需要用户确认
- **取消操作**：用户随时可以取消正在执行的工具调用

---

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

---

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

---

## 部署到服务器（10 元/月方案）

买个便宜的云服务器，比如 [灵梦云](https://idc.np4.cn/) 10 元/月的轻量机：

```bash
git clone --recurse-submodules https://github.com/Micro-Mood/Orion.git
cd Orion
pip install -r requirements.txt
pip install -r axon/requirements.txt
cp config.example.json config.json
# 编辑 config.json，填 API Key

export ORION_HOST="0.0.0.0"
cd src && python main.py
```

配个 Nginx 反向代理 + HTTPS，就能在手机上随时访问。

> 前端自动检测 Base Path，可以放在 `https://你的域名/orion/` 下面和其他服务共存。

---

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

---

## 内置工具

通过 [Axon MCP Server](https://github.com/Micro-Mood/Axon) 提供：

| 分类 | 工具 |
|------|------|
| **文件**（12） | `read_file` · `write_file` · `delete_file` · `copy_file` · `move_file` · `create_directory` · `delete_directory` · `move_directory` · `list_directory` · `stat_path` · `replace_string_in_file` · `multi_replace_string_in_file` |
| **命令**（10） | `run_command` · `create_task` · `stop_task` · `del_task` · `task_status` · `list_tasks` · `read_stdout` · `read_stderr` · `write_stdin` · `wait_task` |
| **搜索**（3） | `find_files` · `search_text` · `find_symbol` |
| **系统**（1） | `get_system_info` |
| **网络**（1） | `fetch_webpage` |

---

## 项目结构

```
Orion/
├── config.example.json
├── requirements.txt
├── axon/                   # Axon MCP Server（git 子模块）
├── src/
│   ├── main.py             # 入口
│   ├── server.py           # FastAPI + WebSocket
│   ├── engine.py           # AI 引擎（工具循环 + 压缩记忆 + fork）
│   ├── memory.py           # 长期记忆归档
│   ├── llm.py              # LLM 客户端（模型降级）
│   ├── mcp_client.py       # MCP TCP 客户端
│   ├── axon_manager.py     # Axon 子进程管理
│   ├── config.py           # 配置
│   ├── context.py          # 对话上下文 + 工具注册表
│   ├── prompt.py           # 系统提示词
│   ├── store.py            # 会话持久化（消息/上下文分离 + fork）
│   ├── tools.py            # 工具注册表（紧凑描述 + Schema）
│   ├── prompts/
│   │   └── system.md       # 系统提示词模板
│   └── web/                # 前端 (Vue 3 + CodeMirror 6)
├── data/                   # 运行时数据（gitignore）
├── workspace/              # 默认工作目录（gitignore）
└── docs/
```

---

## 安全性

- **密码认证** — bcrypt + JWT
- **路径沙箱** — 文件操作限制在工作区内
- **危险命令拦截** — 50+ 种危险命令模式自动拦截
- **敏感数据隔离** — 密钥存在 `config.json`（已 gitignore）

---

## 许可证

[MIT](LICENSE)