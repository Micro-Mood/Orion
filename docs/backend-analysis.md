# Orion 后端架构分析与实现指南

> 这份文档是多轮深度分析的结论汇总，供后续开发参考。
> 写于 2026-04-09，当时前端已完成，后端尚未开始。

---

## 一、项目定位

Orion 是一个**类 Copilot 的个人 AI 助手**。Copilot 是写代码的，Orion 是过日子的。

核心理念：
- 自然对话，不是命令行
- AI 自己决定什么时候用工具、怎么用
- 文件就是记忆，AI 通过读写文件来记住任何事情
- 自驱动多轮循环，不是简单的一问一答

## 二、架构总览

```
用户 ←→ 前端（Vue 3 CDN，已完成）
         ↕ WebSocket (port 8080)
    后端 API Server（FastAPI + Uvicorn）
       ↕                    ↕
  LLM API（百炼）        Axon MCP Server（TCP:9100）
  FIFO 降级               38 个 JSON-RPC 方法
  flash→turbo→plus        文件/搜索/命令/系统
```

### 仓库结构（目标）

```
Orion/
├── axon/                  ← git submodule (Axon MCP Server)
├── src/
│   ├── server.py          ← FastAPI + WebSocket，对外 API（已有 mock 版）
│   ├── engine.py          ← 自驱动循环（核心）
│   ├── llm.py             ← LLM 调用 + 流式 + FIFO 降级
│   ├── context.py         ← 上下文 FIFO 滑动窗口
│   ├── tools.py           ← 工具注册表（映射 Axon 38 个方法）
│   ├── store.py           ← 会话持久化
│   ├── prompt.py          ← System Prompt 管理
│   ├── config.py          ← 配置管理
│   ├── main.py            ← 启动入口（已有）
│   └── web/               ← 前端（已完成）
│       ├── index.html
│       ├── style.css
│       └── app.js
├── .env                   ← API key（gitignore）
├── .gitignore             ← 已有
├── requirements.txt       ← 已有（需扩充）
└── docs/
    ├── requirements.md    ← 需求文档（已有）
    └── backend-analysis.md ← 本文件
```

## 三、Axon MCP — 底座接口

Axon 是独立进程，通过 TCP `127.0.0.1:9100` 提供 JSON-RPC 2.0 接口。
Axon 应作为 git submodule 引入，Orion 使用其自带的 SDK (`MCPClient`)。

### 连接协议

```
TCP 127.0.0.1:9100
JSON-RPC 2.0，行分隔（\n）
请求：{"jsonrpc": "2.0", "method": "read_file", "params": {"path": "..."}, "id": 1}
响应：{"jsonrpc": "2.0", "result": {"status": "success", "data": {...}}, "id": 1}
```

### Axon 全部 38 个方法

#### read 模块（4 个）

| 方法 | 必填参数 | 可选参数 | 返回 |
|------|---------|---------|------|
| `read_file` | `path:str` | `encoding="utf-8"`, `range:tuple`, `max_size=1048576`, `timeout=30000` | `data.content, data.encoding, data.size, data.checksum` |
| `list_directory` | `path:str` | `limit=100`, `offset=0`, `pattern`, `recursive=False`, `max_depth=3`, `include_hidden=False`, `sort_by="name"`, `sort_order="asc"` | `data.items[], data.pagination` |
| `stat_path` | `path:str` | `follow_symlinks=True` | `data.exists, data.type, data.size, data.modified, data.permissions` |
| `exists` | `path:str` | — | `data.exists:bool, data.path` |

#### search 模块（3 个）

| 方法 | 必填参数 | 可选参数 | 返回 |
|------|---------|---------|------|
| `search_files` | `pattern:str`, `root_dir:str` | `max_results=100`, `recursive=True`, `file_types`, `exclude_patterns`, `min_size`, `max_size`, `modified_after/before` | `data.results[], data.statistics` |
| `search_content` | `query:str`, `root_dir:str` | `file_pattern`, `max_files=50`, `max_matches_per_file=10`, `case_sensitive=False`, `whole_word=False`, `context_lines=2`, `is_regex=False` | `data.results[].matches[], data.statistics` |
| `search_symbol` | `symbol:str`, `root_dir:str` | `symbol_type`, `language`, `max_results=50`, `include_definitions=True`, `include_references=False`, `exact_match=True` | `data.results[]` |

#### edit 模块（11 个）

| 方法 | 必填参数 | 可选参数 |
|------|---------|---------|
| `create_file` | `path:str` | `content=""`, `encoding="utf-8"`, `overwrite=False` |
| `write_file` | `path:str`, `content:str` | `encoding="utf-8"`, `create_parents=True` |
| `delete_file` | `path:str` | `backup=False`, `backup_dir`, `permanent=False` |
| `move_file` | `source:str`, `destination:str` | `overwrite=False`, `copy_permissions=True`, `preserve_timestamps=True` |
| `copy_file` | `source:str`, `destination:str` | `overwrite=False` |
| `replace_range` | `path:str`, `range:tuple`, `new_text:str` | `encoding="utf-8"`, `unit="bytes"` |
| `insert_text` | `path:str`, `position:int`, `text:str` | `encoding="utf-8"`, `unit="bytes"` (支持 `bytes/chars/line`) |
| `delete_range` | `path:str`, `range:tuple` | `encoding="utf-8"`, `unit="bytes"` |
| `apply_patch` | `path:str`, `patch:str` | `encoding="utf-8"`, `dry_run=False`, `reverse=False` |
| `create_directory` | `path:str` | `recursive=True`, `mode="755"` |
| `delete_directory` | `path:str` | `recursive=False`, `force=False` |
| `move_directory` | `source:str`, `destination:str` | `overwrite=False`, `copy_permissions=True` |

#### execute 模块（13 个）

| 方法 | 必填参数 | 可选参数 | 说明 |
|------|---------|---------|------|
| `run_command` | `command:str` | `cwd`, `timeout=30000`, `env:dict` | 便捷方法，create+start+wait 合一 |
| `create_task` | `command:str` | `args`, `cwd`, `env`, `shell=True`, `timeout`, `stdin`, `detached=False`, `priority="normal"` | 创建进程 |
| `start_task` | `task_id:str` | — | 启动进程 |
| `stop_task` | `task_id:str` | `signal_name="CTRL_C"`, `timeout=5000` | 优雅停止 |
| `kill_task` | `task_id:str` | — | 强制终止 |
| `get_task` | `task_id:str` | — | 状态+CPU/内存 |
| `list_tasks` | — | `filter="all"`, `limit=50` | `all/active/completed/failed` |
| `wait_task` | `task_id:str` | `timeout=30000` | 等待完成 |
| `write_stdin` | `task_id:str`, `data:str` | `encoding="utf-8"`, `eof=False` | |
| `stream_stdout` | `task_id:str` | `max_bytes=8192`, `timeout=1000`, `encoding="utf-8"` | |
| `stream_stderr` | `task_id:str` | （同 stdout） | |
| `attach_task` | `task_id:str` | `stream_stdout=True`, `stream_stderr=True`, `buffer_size=4096` | |
| `detach_task` | `task_id:str` | — | |

#### system 模块（7 个）

| 方法 | 参数 | 返回 |
|------|------|------|
| `ping` | 无 | `data.pong, data.timestamp` |
| `get_version` | 无 | `data.version, data.protocol, data.python` |
| `get_methods` | 无 | `data.methods[], data.count` |
| `get_config` | 无 | `data.workspace, data.performance, data.server` |
| `set_workspace` | `root_path:str`, 可选 `persist=True`, `config_path`, `reset_cache=True` | |
| `get_stats` | 无 | 缓存命中率等 |
| `clear_cache` | 无 | |

### Orion 实际使用频率

```
高频（每次对话可能触及）:
  read_file, write_file, create_file, search_content, list_directory, run_command

中频:
  search_files, stat_path, exists, delete_file, move_file, copy_file
  replace_range, insert_text, create_directory

低频:
  delete_range, apply_patch, move_directory, delete_directory
  search_symbol, 进程管理 9 个

系统:
  ping（健康检查）, set_workspace（启动时）, get_methods（获取可用列表）
```

## 四、AutomateX 复用指南

AutomateX 是同一作者的代码代理项目，Orion 可以从中复用大量基础设施。

### 可直接复用的模块

#### 1. `chat/interface.py` → Orion 的 `llm.py`（~240 行）

```python
class OpenAIChatAPI:
    def __init__(self, api_key, base_url, model)
    def chat(self, messages, temperature=1.0, timeout=180, max_retries=3,
             stream=True, show_reasoning=True, on_stream=None,
             tools=None, tool_choice=None) -> Union[str, Dict]
```

关键特性：
- 统一 OpenAI 兼容接口（百炼/DeepSeek/Kimi 都支持）
- 流式处理：SSE 格式 `data: {...}`，支持 reasoning_content（思考链）
- 重试：指数退避 `min(2^attempt, 10)` 秒
- 降级：tools 参数被 400 拒绝时自动去掉 tools 重试
- Token 追踪：`self.last_usage` 记录 prompt/completion/total tokens
- **但没有 FIFO 模型降级！** Orion 需要新增：flash → turbo → plus 自动切换

#### 2. `context.py` → Orion 的 `context.py`（~95 行）

```python
class Context:
    system_msg: Optional[Message]    # 不计入 FIFO，始终在最前
    history: List[Message]           # FIFO 队列
    max_history: int = 20            # 保留最近 20 条消息（约 10 轮对话）
    phase: Phase                     # SELECT/PARAMS/EXEC/RESULT
    selected_tools: List[str]

    def add_user(content)            # 添加后自动 _trim()
    def add_assistant(content)
    def build_messages() -> list     # [system] + history → 给 API
    def token_estimate() -> int      # len(content) // 4 粗估
```

直接复用，改 Phase 枚举适配 Orion 阶段。

#### 3. `store.py` → Orion 的 `store.py`（~370 行）

```python
class TaskStore:
    # 任务与消息分离存储
    # store.json — 所有任务元数据
    # messages/{task_id}.json — 每个任务的消息历史

    # 原子写入（Windows 兼容）: 写 .tmp → copy .bak → move
    # 消息大小控制: 单条 50KB, 总数 500 条, 文件 5MB
    # 线程安全: threading.RLock()
```

把 `Task` 改为 `Session`，任务 CRUD 改为会话 CRUD，其余原子写入/消息分离/大小控制直接复用。

#### 4. `tools.py` 框架 → Orion 的 `tools.py`（~350 行）

```python
@dataclass
class ToolParam:
    name: str; type: str; desc: str; required: bool; default: Optional[str]

@dataclass
class Tool:
    name: str; desc: str; params: List[ToolParam]; category: str
    def to_compact(self) -> str
    # 输出紧凑格式: "name|desc|param:type*说明;param:type=default,说明"
    # 极大压缩 token（vs 完整 JSON schema）

TOOLS: Dict[str, Tool] = {}  # 全局注册表
```

框架完全复用，工具定义重新注册（38 个 Axon 方法 + done/fail/ask 控制指令）。

#### 5. `config/loader.py` → Orion 的 `config.py`（~190 行）

```python
class ConfigManager:  # 单例
    user: UserConfig   # api_key, base_url, model, working_directory
    sys: SysConfig     # mcp host/port, max_iterations, log_level
    # 双配置: sys_config.json（系统） + user_config.json（用户）
    # 优先级: 环境变量 > 用户配置 > 系统配置 > 默认值
```

#### 6. `engine.py` 中的解析器（可直接提取）

```python
def parse_tool_select(response: str) -> List[str]     # 从 AI 回复解析 {"select": [...]}
def parse_tool_call(response: str) -> Optional[Dict]   # 解析 {"call": "tool", "param": "value"}
def parse_all_tool_calls(response: str) -> List[Dict]  # 解析多个工具调用
```

纯正则解析，与业务无关，直接复用。

### 需要重写的

| 模块 | 原因 |
|------|------|
| `engine.py` 主循环 | 生命周期不同：AutomateX 任务有终态，Orion 会话持续存在 |
| `prompt/select.md` | 角色完全不同：代码代理 vs 生活助手 |
| `api.py` | AutomateX 是 CLI 门面，Orion 是 WebSocket 服务 |
| `mcp_client.py` | 不需要——Axon 自带 SDK |

### 不需要的

| AutomateX 部分 | 原因 |
|---|---|
| `TodoItem / NeedInputInfo` | Orion 没有 TODO 子任务系统 |
| `run_interactive()` | Orion 不是 CLI |
| `local_*` fallback | Axon 不可用时直接报错 |
| `_validate_command_locally` | 安全由 Axon 中间件保证 |

## 五、Orion 引擎设计（与 AutomateX 的关键差异）

### AutomateX 的两阶段循环

```
SELECT → AI 从工具名列表中选工具 → {"select": ["read_file", "search_content"]}
PARAMS → 注入选中工具的精简描述 → AI 填参数 → {"call": "read_file", "path": "..."}
EXEC   → 执行 → 结果追回上下文 → 回到 SELECT
```

核心优化：SELECT 阶段只传工具名（~100 tokens），PARAMS 阶段才传选中工具的参数说明。比一次性传所有工具的完整 schema（~5000 tokens）省很多。

### Orion 需要的调整

| 差异点 | AutomateX | Orion |
|--------|-----------|-------|
| 循环生命周期 | 任务有终态（COMPLETED/FAILED） | `done` 只结束当前轮，会话一直存在 |
| 用户交互 | `ask` → 暂停任务 → 用户回复 → 恢复 | 同理，但更频繁（对话场景） |
| TODO 系统 | 有，AI 管理子任务 | **不需要** |
| 纯对话模式 | AI 可以不调工具直接回答 | **同理，且是主要场景** |
| 工具调用方式 | 纯文本 JSON（不用 function calling） | **沿用**，省 Token，兼容所有模型 |

### Orion 引擎循环

```
用户消息 → engine.run(session_id, content)
  ├─ context.add_user(content)
  ├─ context.build_messages() → [system, ...history]
  ├─ llm.chat(messages, stream=True)
  ├─ 解析 AI 返回:
  │   ├─ 纯文本 → 直接完成（对话式回复）
  │   ├─ {"select": [...]} → 注入工具描述 → AI 填参数
  │   ├─ {"call": "done", "summary": "..."} → 结束当前轮
  │   ├─ {"call": "fail", "reason": "..."} → 报错
  │   ├─ {"call": "ask", "question": "..."} → 暂停等用户
  │   └─ {"call": "tool_name", ...} → Axon 执行 → 结果回上下文 → 继续
  └─ 流式推送: on_stream(delta), on_tool_start/end, on_model_info
```

## 六、模型 FIFO 降级（AutomateX 没有，需新建）

```
请求 → flash(最便宜) → 成功 → 返回
                      → 失败 → turbo(备选) → 成功 → 返回
                                            → 失败 → plus(兜底) → 返回/报错
```

| 优先级 | 模型 | 输入/输出价格（/百万Token） |
|--------|------|---------------------------|
| 1 | qwen-flash | 0.15 / 1.5 元 |
| 2 | qwen-turbo | 0.3 / 0.6 元 |
| 3 | qwen-plus | 0.8 / 2 元 |

降级触发条件：API 报错 / 超时 / 额度用尽（不是 400 参数错误）。

## 七、WebSocket API（前端 ←→ 后端）

### 已有（前端已实现对应处理）

| 消息类型 | 方向 | 说明 |
|---------|------|------|
| `get_sessions` | → | 获取列表 |
| `create_session` | → | 新建会话 |
| `delete_session` | → | 删除 |
| `update_session_title` | → | 改标题 |
| `get_messages` | → | 获取历史 |
| `send_message` | → | 发送消息（当前触发 mock，需改为真实引擎） |
| `session_list` | ← | 返回列表 |
| `session_created` | ← | 广播新建 |
| `session_deleted` | ← | 广播删除 |
| `session_messages` | ← | 返回消息 |
| `message_start` | ← | AI 开始回复 |
| `stream_delta` | ← | 流式文字 |
| `message_end` | ← | 回复结束 |
| `tool_start` | ← | 工具开始（含 tool_name, params） |
| `tool_end` | ← | 工具结束（含 result, success, duration） |
| `thinking` | ← | 思考中 |

### 需要新增

| 消息类型 | 方向 | 说明 |
|---------|------|------|
| `continue_input` | → | AI ask 后用户回复 |
| `cancel` | → | 用户取消当前处理 |
| `ask` | ← | AI 需要用户输入 |
| `error` | ← | 引擎错误 |
| `model_info` | ← | 当前模型名（降级时会变） |

## 八、分层实现计划

| 步骤 | 模块 | 依赖 | 验证方式 |
|------|------|------|----------|
| **L0** | Axon 作为 git submodule | 无 | `git submodule add` |
| **L1** | `llm.py` — LLM 调用 + 流式 + FIFO 降级 | .env (API key) | 脚本测试：聊天 + stream + 降级触发 |
| **L2** | `context.py` — FIFO 滑动窗口 | 无 | 单测：add/trim/build 正确 |
| **L3** | `tools.py` — 工具注册表 | 无 | 打印工具表，和 Axon 38 个对齐 |
| **L4** | `prompt.py` — System Prompt | 无 | 打印完整 prompt |
| **L5** | `engine.py` — 自驱动循环 | L1+L2+L3+L4+Axon | 脚本测试：输入 → AI 自驱动 → 工具调用 |
| **L6** | `server.py` 改造 | L5 | 前端完整交互 |
| **L7** | `store.py` — 会话持久化 | L6 | 重启后数据在 |

L1 和 L2 互相独立可以同时做，L3 和 L4 也是。L5 是核心整合层。

## 九、前端现状（已完成部分）

- **框架**: Vue 3 CDN（无构建步骤），VS Code Dark 主题
- **布局**: VS Code 工作台（Activity Bar + Sidebar + Editor Area）
- **功能**: 多会话、Copilot 风格工具调用展示（9 种工具图标）、流式消息、Markdown 渲染、代码高亮
- **头像**: Bean 用户头像（炭灰/珊瑚 `#2c2c2c`+`#e06c75`），Marble AI 头像（冰川蓝 `#0d4a7a`+`#74b9ff`+`#a8d8ea`）
- **响应式**: 手机优先（侧边栏 overlay、touch-friendly）
- **状态栏**: 左=模型名，右=未读数
- **标题栏**: 简化（toggle + 会话标题）
- **文件浏览器**: 侧边栏"工作区"视图 — 还是 placeholder
- **外部库**: marked.js（Markdown）、highlight.js（代码高亮）

## 十、部署环境

| 项目 | 要求 |
|------|------|
| 服务器 | 1 核 1GB 内存 |
| Python | 3.10+ |
| 外部依赖 | 百炼 API（OpenAI 兼容） |
| 数据库 | 无（文件系统即存储） |
| 端口 | Orion: 8080, Axon: 9100 |
| 内存预算 | < 100MB（FastAPI + Axon） |

## 十一、待确认的决策点

1. **Axon 引入方式**: git submodule 还是只复制 SDK 文件？
2. **Axon 端口**: AutomateX 默认 8080，需求文档写 9100，建议 9100 避免和 Orion 冲突
3. **API Key 存储**: .env 文件（dotenv 加载）
4. **工具调用方式**: 沿用 AutomateX 的两阶段纯文本（不用 function calling），省 Token
5. **持久化**: V1 用 JSON 文件（和 AutomateX 一样），后续可换 SQLite
