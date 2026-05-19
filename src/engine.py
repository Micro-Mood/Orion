"""
Orion AI 引擎
=============

单阶段原生 Tool Calling 循环 + register_tool 元工具：
  始终可用: register_tool / unregister_tool
  模型调 register_tool(names=[...]) 后，后续轮才能调用该工具
  纯文本回复不结束本轮——必须显式调 done
  已注册工具空闲 N 轮后自动卸载（TTL，可配）
  已注册列表按会话持久化到 store，跨用户回合复用

特性:
- 原生 OpenAI tool_calls 协议，无自定义 JSON 解析
- 流式输出（纯文本实时推送）
- 危险工具需用户确认
- 取消操作
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Dict, List, Optional, Tuple

from context import Context, Message
from llm import LLMClient, LLMError
from mcp_client import MCPClient
from prompt import build_system_prompt
from store import SessionStore
from tools import (
    TOOLS,
    get_always_available_schemas,
    get_schemas_for_registered,
)
import memory as memory_mod

logger = logging.getLogger(__name__)


# ==================== 数据结构 ====================

@dataclass
class EngineCallbacks:
    """引擎回调，用于向 WebSocket 推送事件"""
    on_text: Optional[Callable[[str], Awaitable[None]]] = None
    on_thinking: Optional[Callable[[str], Awaitable[None]]] = None
    on_tool_start: Optional[Callable[[str, Dict], Awaitable[None]]] = None
    on_tool_end: Optional[Callable[[str, Dict, bool, int], Awaitable[None]]] = None
    on_model_info: Optional[Callable[[str], Awaitable[None]]] = None
    on_title_update: Optional[Callable[[str], Awaitable[None]]] = None
    on_usage: Optional[Callable[[int], Awaitable[None]]] = None


@dataclass
class ToolCallRecord:
    """工具调用记录"""
    name: str
    params: Dict[str, Any]
    success: bool
    result: str
    duration_ms: int


@dataclass
class EngineResult:
    """引擎运行结果"""
    text: str
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    model: str = ""
    is_ask: bool = False
    is_pending_confirm: bool = False   # 危险工具等待确认
    pending_tool_calls: List[Dict] = field(default_factory=list)  # 待确认的 params_tool_calls
    is_error: bool = False
    cancelled: bool = False
    options: List[str] = field(default_factory=list)


# ==================== 引擎 ====================

class OrionEngine:
    """
    Orion AI 引擎

    每条用户消息触发一次 run()。
    引擎直接管理持久化: 所有中间消息写入 store.context[]，
    确保多轮上下文连续。
    """

    def __init__(self, llm: LLMClient, mcp: MCPClient, store: SessionStore,
                 max_iterations: int = 30,
                 working_directory: str = "",
                 read_file_max_lines: int = 200,
                 tool_ttl_rounds: int = 5,
                 context_window: int = 128000,
                 compress_at: float = 0.85,
                 context_recent_n: int = 8,
                 memory_dir: str = ".orion"):
        self.llm = llm
        self.mcp = mcp
        self.store = store
        self.max_iterations = max_iterations
        self.read_file_max_lines = read_file_max_lines
        self.tool_ttl_rounds = tool_ttl_rounds
        self.cwd = working_directory or "."
        self.context_window = context_window
        self.compress_at = compress_at
        self.context_recent_n = max(1, context_recent_n)
        self.memory_dir = memory_dir or ".orion"

        # 取消标记: session_id → bool
        self._cancel_flags: Dict[str, bool] = {}

    def cancel(self, session_id: str):
        """取消指定会话的处理"""
        self._cancel_flags[session_id] = True

    async def run(self, session_id: str, user_content,
                  callbacks: EngineCallbacks,
                  auto_confirm_dangerous: bool = False) -> EngineResult:
        """
        处理一条用户消息

        单阶段 register_tool 循环:
        1. 保存用户消息到 store.context[]（user_content=None 时跳过，用于 confirm resume）
        2. 从 store.context[] 恢复完整历史到 Context
        3. 恢复会话级 registered_tools
        4. 循环：模型一次调用 → 处理 tool_calls / 纯文本不结束
        5. 返回 EngineResult
        """
        self._cancel_flags[session_id] = False

        # 1. 保存用户消息（resume 时 user_content=None，跳过写入）
        if user_content is not None:
            self.store.add_context(session_id, "user", user_content)

        # 2. 构建上下文（从 store 恢复完整历史; token 压缩在 prepare 阶段触发）
        ctx = Context()
        memory_section = memory_mod.build_memory_section(self.cwd, self.memory_dir)
        ctx.set_system(build_system_prompt(
            self.cwd, self.tool_ttl_rounds, memory_index=memory_section,
        ))

        all_ctx = self.store.get_context(session_id)
        for msg in all_ctx:
            role = msg.get("role", "")
            content = msg.get("content") or ""
            if role == "user":
                ctx.add_user(content)
            elif role == "assistant":
                if msg.get("tool_calls"):
                    ctx.add_tool_call_assistant(msg["tool_calls"],
                                                msg.get("content"))
                else:
                    ctx.add_assistant(content)
            elif role == "tool":
                ctx.add_tool_result(
                    msg.get("tool_call_id", ""),
                    msg.get("name", ""),
                    content,
                )
            elif role == "system":
                ctx.add_system_note(content)

            # 恢复已确认的危险工具（兼容旧格式）
            meta = msg.get("metadata", {})
            if meta.get("confirmed_tools"):
                for t in meta["confirmed_tools"]:
                    ctx.confirmed_tools.add(t)

        # 2b. 恢复会话级已注册工具表
        ctx.registered_tools = self.store.get_session_registered_tools(session_id)

        # 3. 确保 MCP 连接
        await self._ensure_mcp()

        tool_records: List[ToolCallRecord] = []
        last_model = ""
        iteration = 0
        consecutive_failures = 0

        def persist_registered():
            self.store.set_session_registered_tools(
                session_id, dict(ctx.registered_tools)
            )

        try:
            while self.max_iterations <= 0 or iteration < self.max_iterations:
                if self._cancel_flags.get(session_id, False):
                    return EngineResult("Cancelled", tool_records,
                                        model=last_model, cancelled=True)

                iteration += 1
                logger.debug(f"[{session_id}] 迭代 {iteration} "
                             f"registered={list(ctx.registered_tools.keys())}")

                # 上下文压缩触发: token 占比超阈值时归档旧历史
                if ctx.needs_compression(self.context_window, self.compress_at):
                    try:
                        await self._compress_context(session_id, ctx)
                    except Exception as e:
                        logger.error(f"[{session_id}] 压缩上下文失败: {e}",
                                     exc_info=True)

                # 构建本轮可用工具：始终可用 + 已注册
                tools = (get_always_available_schemas()
                         + get_schemas_for_registered(
                             list(ctx.registered_tools.keys())))

                # 单次 LLM 调用（流式）
                full_text, tool_calls, model = await self._stream_round(
                    ctx, tools, callbacks
                )
                last_model = model

                # 没有 tool_calls：纯文本回复 = 本轮结束
                if not tool_calls:
                    if full_text:
                        ctx.add_assistant(full_text)
                        self.store.add_context(
                            session_id, "assistant", full_text,
                            metadata={"text_only": True}
                        )
                    persist_registered()
                    return EngineResult(
                        full_text or "", tool_records,
                        model=last_model,
                        is_error=not full_text,
                    )

                # 写入 assistant tool_calls
                ctx.add_tool_call_assistant(tool_calls, full_text or None)
                self.store.add_context_entry(session_id, {
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": tool_calls,
                    "metadata": {"iter": iteration},
                })

                early_return: Optional[EngineResult] = None

                for tc in tool_calls:
                    if self._cancel_flags.get(session_id, False):
                        early_return = EngineResult(
                            "Cancelled", tool_records,
                            model=last_model, cancelled=True
                        )
                        break

                    name = tc["function"]["name"]
                    args_str = tc["function"].get("arguments", "") or ""
                    try:
                        args = (json.loads(args_str)
                                if args_str.strip() else {})
                    except json.JSONDecodeError:
                        args = {}
                    tc_id = tc.get("id") or f"call_{name}_{iteration}"

                    # ---- meta：register_tool / unregister_tool ----
                    if name == "register_tool":
                        raw_names = args.get("names") or []
                        if isinstance(raw_names, str):
                            raw_names = [raw_names]
                        # 过滤无效名（未知工具）+ 拒绝 meta（自己已可用）
                        valid = [n for n in raw_names
                                 if n in TOOLS
                                 and TOOLS[n].category not in ("meta", "ctrl")]
                        unknown = [n for n in raw_names if n not in TOOLS]
                        new = ctx.register(valid)
                        persist_registered()
                        parts = []
                        if new:
                            parts.append(f"registered: {', '.join(new)}")
                        already = [n for n in valid if n not in new]
                        if already:
                            parts.append(f"already-registered (idle reset): {', '.join(already)}")
                        if unknown:
                            parts.append(f"unknown (ignored): {', '.join(unknown)}")
                        result = "; ".join(parts) or "no tools provided"
                        ctx.add_tool_result(tc_id, name, result)
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": result,
                            "metadata": {"success": True},
                        })
                        consecutive_failures = 0
                        continue

                    if name == "unregister_tool":
                        raw_names = args.get("names") or []
                        if isinstance(raw_names, str):
                            raw_names = [raw_names]
                        removed = ctx.unregister(raw_names)
                        persist_registered()
                        result = (f"unregistered: {', '.join(removed)}"
                                  if removed else "nothing to unregister")
                        ctx.add_tool_result(tc_id, name, result)
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": result,
                            "metadata": {"success": True},
                        })
                        consecutive_failures = 0
                        continue

                    # ---- 必须已注册才能调用（ctrl 类始终可用，跳过）----
                    if (name not in ctx.registered_tools
                            and name in TOOLS
                            and TOOLS[name].category != "ctrl"):
                        err = (f"Tool `{name}` is not registered. "
                               f"Call register_tool(names=[\"{name}\"]) first, "
                               f"then call it in the next round.")
                        ctx.add_tool_result(tc_id, name, f"Error: {err}")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": f"Error: {err}",
                            "metadata": {"success": False},
                        })
                        consecutive_failures += 1
                        continue

                    # ---- ctrl: ask / fail / set_session_title ----
                    if name == "ask":
                        question = args.get("question", "")
                        if question:
                            await self._emit_text(callbacks, question)
                            self.store.add_context(
                                session_id, "assistant", question,
                                metadata={"phase": "ask"}
                            )
                        raw_opts = args.get("options", [])
                        options = ([str(o) for o in raw_opts]
                                   if isinstance(raw_opts, list) else [])
                        ctx.add_tool_result(tc_id, name, "ok")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": "ok",
                            "metadata": {"success": True},
                        })
                        ctx.touch_tool(name)
                        early_return = EngineResult(
                            question, tool_records, model=last_model,
                            is_ask=True, options=options
                        )
                        break

                    if name == "fail":
                        reason = args.get("reason", "操作失败")
                        await self._emit_text(callbacks, reason)
                        self.store.add_context(
                            session_id, "assistant", reason,
                            metadata={"phase": "fail"}
                        )
                        ctx.add_tool_result(tc_id, name, "ok")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": "ok",
                            "metadata": {"success": True},
                        })
                        ctx.touch_tool(name)
                        early_return = EngineResult(
                            reason, tool_records, model=last_model,
                            is_error=True
                        )
                        break

                    if name == "set_session_title":
                        title = args.get("title", "")
                        if title and callbacks.on_title_update:
                            try:
                                await callbacks.on_title_update(title)
                            except Exception:
                                pass
                        ctx.add_tool_result(tc_id, name, "ok")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": "ok",
                            "metadata": {"success": True},
                        })
                        ctx.touch_tool(name)
                        continue

                    # ---- 真正的 Axon 工具 ----
                    tool = TOOLS.get(name)
                    if not tool:
                        err = f"Unknown tool: {name}"
                        ctx.add_tool_result(tc_id, name, f"Error: {err}")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": f"Error: {err}",
                            "metadata": {"success": False},
                        })
                        consecutive_failures += 1
                        continue

                    # 危险工具确认
                    if tool.dangerous and name not in ctx.confirmed_tools \
                            and not auto_confirm_dangerous:
                        # 中断本轮，前端渲染确认 UI
                        # 注意 tool_calls 已经写入 ctx 了
                        early_return = EngineResult(
                            "", tool_records, model=last_model,
                            is_pending_confirm=True,
                            pending_tool_calls=[tc],
                        )
                        break

                    record = await self._exec_tool(name, args, callbacks)
                    tool_records.append(record)

                    if record.success:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1

                    result_content = self._format_result(
                        name, record.success, record.result
                    )
                    ctx.add_tool_result(tc_id, name, result_content)
                    self.store.add_context_entry(session_id, {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": name,
                        "content": result_content,
                        "metadata": {
                            "success": record.success,
                            "duration_ms": record.duration_ms,
                        },
                    })
                    ctx.touch_tool(name)

                    if consecutive_failures >= 3:
                        note = ("Multiple consecutive tool failures. "
                                "Please check parameters or try a different approach.")
                        ctx.add_system_note(note)
                        self.store.add_context(
                            session_id, "system", note,
                            metadata={"type": "system_inject"}
                        )
                        consecutive_failures = 0
                        break

                if early_return is not None:
                    # done/ask/fail/cancel/pending_confirm 退出前先持久化注册表
                    persist_registered()
                    return early_return

                # 本轮收尾：老化已注册工具
                evicted = ctx.age_and_evict(self.tool_ttl_rounds)
                if evicted:
                    persist_registered()
                    logger.debug(f"[{session_id}] evicted: {evicted}")

            # 达到最大迭代
            logger.warning(f"[{session_id}] 达到最大迭代 {self.max_iterations}")
            persist_registered()
            return EngineResult(
                f"Reached max steps ({self.max_iterations}). "
                f"Please simplify your request and retry.",
                tool_records, model=last_model, is_error=True
            )

        except LLMError as e:
            logger.error(f"[{session_id}] LLM 错误: {e}")
            return EngineResult(f"AI service error: {e}", tool_records,
                                model=last_model, is_error=True)
        except Exception as e:
            logger.error(f"[{session_id}] 引擎异常: {e}", exc_info=True)
            return EngineResult(f"Internal error: {e}", tool_records,
                                model=last_model, is_error=True)
        finally:
            self._cancel_flags.pop(session_id, None)

    # ==================== 上下文压缩 ====================

    async def _compress_context(self, session_id: str, ctx: Context):
        """触发上下文压缩: 把 history[:-recent_n] 归档为 .orion/<id>.md, 替换 history。

        触发时机由调用方判断 (ctx.needs_compression)。本方法不返回值,
        失败时抛异常由调用方捕获。
        """
        recent_n = self.context_recent_n
        if len(ctx.history) <= recent_n + 1:
            return  # 历史太短, 没必要压缩

        to_archive = ctx.history[:-recent_n]
        recent = ctx.history[-recent_n:]

        # 序列化待归档历史为简短文本 (避免一次性发太大)
        snippets: List[str] = []
        for m in to_archive:
            role = m.role
            content = (m.content or "")[:1500]
            if m.tool_calls:
                try:
                    tc_brief = json.dumps(
                        [{"name": tc.get("function", {}).get("name"),
                          "args": tc.get("function", {}).get("arguments", "")[:300]}
                         for tc in m.tool_calls],
                        ensure_ascii=False,
                    )
                except Exception:
                    tc_brief = "[tool_calls]"
                snippets.append(f"[{role} tool_calls]: {tc_brief}")
                if content:
                    snippets.append(f"[{role} text]: {content}")
            elif role == "tool":
                snippets.append(f"[tool:{m.name}]: {content}")
            else:
                snippets.append(f"[{role}]: {content}")

        history_text = "\n".join(snippets)
        # 二次截断, 防止超过模型上下文
        max_chars = self.context_window * 2  # 粗略字符上限
        if len(history_text) > max_chars:
            history_text = history_text[:max_chars] + "\n...[truncated]"

        sys_prompt = (
            "你是对话压缩助手。请把下面这段 AI Agent 与用户的对话历史压缩为一段长期记忆, "
            "供未来会话快速回顾。必须严格输出 JSON 对象 (不要 ```), 字段如下:\n"
            "{\n"
            '  "title": "<=15字的中文标题, 概括主题",\n'
            '  "summary": "Markdown 段落, 客观摘要所做工作/结论/未决事项",\n'
            '  "user_quotes": "重要的用户原话摘录 (Markdown 列表, 可为空)",\n'
            '  "notes": "AI 视角的关键观察/教训/注意点 (可为空)"\n'
            "}\n"
            "只输出 JSON, 不要任何额外说明。"
        )
        user_prompt = f"对话历史:\n\n{history_text}"

        try:
            resp = await self.llm.chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.2,
            )
            raw = (resp.content or "").strip()
        except LLMError as e:
            logger.warning(f"[{session_id}] 压缩 LLM 调用失败: {e}")
            return

        # 兼容模型偶尔包裹 ```json 的情况
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].lstrip()
            if raw.endswith("```"):
                raw = raw[:-3]

        title = "历史归档"
        summary = raw
        user_quotes = ""
        notes = ""
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                title = str(obj.get("title") or title)[:30]
                summary = str(obj.get("summary") or "").strip() or summary
                user_quotes = str(obj.get("user_quotes") or "").strip()
                notes = str(obj.get("notes") or "").strip()
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"[{session_id}] 压缩结果非 JSON, 原文存入摘要")

        # 写记忆文件 + 更新索引
        try:
            rel = memory_mod.archive_memory(
                self.cwd, title, summary, user_quotes, notes, self.memory_dir,
            )
        except Exception as e:
            logger.error(f"[{session_id}] 写入记忆失败: {e}")
            return

        archived_count = len(to_archive)
        note_text = (
            f"[已压缩] 早期 {archived_count} 条对话已归档到 `{rel}`。"
            f" 标题: {title}。如需细节请 read_file 加载该文件。"
        )

        # 替换 ctx.history: 注入一条 system_note + 保留 recent
        ctx.history = [
            Message(role="system", content=note_text),
            *recent,
        ]

        # 同步写回 store.context: 系统注入 + recent 原样 (从 store 重新读取以保留原始字段)
        all_ctx = self.store.get_context(session_id)
        if len(all_ctx) > recent_n:
            kept_raw = all_ctx[-recent_n:]
        else:
            kept_raw = all_ctx
        new_entries = [{
            "role": "system",
            "content": note_text,
            "metadata": {"type": "memory_archive", "file": rel,
                         "archived": archived_count},
        }] + list(kept_raw)
        self.store.set_context(session_id, new_entries)

        # 重新加载 system_msg 以包含最新索引
        memory_section = memory_mod.build_memory_section(self.cwd, self.memory_dir)
        ctx.set_system(build_system_prompt(
            self.cwd, self.tool_ttl_rounds, memory_index=memory_section,
        ))

        logger.info(
            f"[{session_id}] 上下文已压缩: archived={archived_count} → {rel}"
        )

    # ==================== LLM 调用 ====================

    async def _ensure_mcp(self):
        """确保 MCP 连接并设置工作目录"""
        if not self.mcp:
            return
        if not self.mcp.connected:
            connected = await self.mcp.connect()
            if not connected:
                logger.warning("Axon MCP Server 未连接，工具调用将不可用")
                return
        if self.cwd and self.cwd != ".":
            await self.mcp.set_workspace(self.cwd)

    async def _stream_round(
        self,
        ctx: Context,
        schemas: List[Dict],
        callbacks: EngineCallbacks,
    ) -> Tuple[str, Optional[List[Dict]], str]:
        """单轮流式 LLM 调用。

        tool_choice="auto"：
        - 纯文本回复 → 实时推送给用户，返回 (text, None, model)
        - tool_calls → 返回 (text, tool_calls, model)
        """
        messages = ctx.build_messages()
        full_text = ""
        tool_calls: Optional[List[Dict]] = None
        model = ""

        try:
            async for chunk in self.llm.chat_stream(
                messages, tools=schemas, tool_choice="auto"
            ):
                model = chunk.model

                if chunk.reasoning and callbacks.on_thinking:
                    try:
                        await callbacks.on_thinking(chunk.reasoning)
                    except Exception:
                        pass

                if chunk.tool_calls:
                    tool_calls = chunk.tool_calls
                    break

                if chunk.content:
                    full_text += chunk.content
                    if callbacks.on_text:
                        try:
                            await callbacks.on_text(chunk.content)
                        except Exception:
                            pass

        except LLMError:
            if not full_text and not tool_calls:
                response = await self.llm.chat(
                    messages, tools=schemas, tool_choice="auto"
                )
                full_text = response.content or ""
                tool_calls = response.tool_calls
                model = response.model

        if callbacks.on_model_info and model:
            try:
                await callbacks.on_model_info(model)
            except Exception:
                pass

        if callbacks.on_usage and self.llm.last_usage:
            try:
                await callbacks.on_usage(self.llm.last_usage.total_tokens)
            except Exception:
                pass

        return full_text, tool_calls, model

    # ==================== 工具执行 ====================

    async def _exec_tool(self, name: str, args: Dict[str, Any],
                         callbacks: EngineCallbacks) -> ToolCallRecord:
        """执行工具并广播事件"""
        params = {k: v for k, v in args.items() if v is not None}

        # read_file 大文件保护
        if (name == "read_file"
                and "line_range" not in params
                and self.read_file_max_lines > 0):
            params["line_range"] = [1, self.read_file_max_lines]

        if callbacks.on_tool_start:
            try:
                await callbacks.on_tool_start(name, params)
            except Exception:
                pass

        t0 = time.perf_counter()

        # 检查 MCP 连接
        if not self.mcp or not self.mcp.connected:
            if self.mcp:
                connected = await self.mcp.ensure_connected()
            else:
                connected = False
            if not connected:
                duration_ms = int((time.perf_counter() - t0) * 1000)
                err = f"{name} 失败: Axon MCP Server 未连接"
                record = ToolCallRecord(name=name, params=params,
                                        success=False, result=err,
                                        duration_ms=duration_ms)
                if callbacks.on_tool_end:
                    try:
                        await callbacks.on_tool_end(
                            name, {"error": err}, False, duration_ms
                        )
                    except Exception:
                        pass
                return record

        mcp_result = await self.mcp.call(name, params)
        duration_ms = int((time.perf_counter() - t0) * 1000)

        if mcp_result.success:
            result_str = json.dumps(
                mcp_result.data or {}, ensure_ascii=False, indent=2
            )
        else:
            result_str = mcp_result.error or "未知错误"

        record = ToolCallRecord(
            name=name, params=params,
            success=mcp_result.success,
            result=result_str,
            duration_ms=duration_ms,
        )

        if callbacks.on_tool_end:
            try:
                result_data = {
                    "success": mcp_result.success,
                    "data": result_str[:500] if mcp_result.success else None,
                    "error": mcp_result.error if not mcp_result.success else None,
                }
                await callbacks.on_tool_end(
                    name, result_data, mcp_result.success, duration_ms
                )
            except Exception:
                pass

        return record

    # ==================== 辅助方法 ====================

    async def _emit_text(self, callbacks: EngineCallbacks, text: str):
        """发送文本到前端"""
        if callbacks.on_text and text:
            try:
                await callbacks.on_text(text)
            except Exception:
                pass

    def _format_result(self, tool: str, success: bool, result: str) -> str:
        """格式化工具执行结果（写入 role=tool 消息，模型可见）"""
        if success:
            max_len = 6000 if tool == "read_file" else 2000
            if len(result) > max_len:
                result = result[:max_len]
                result += f"\n...[truncated, total {len(result)} chars]"
                if tool == "read_file":
                    result += ("\nTip: use line_range to read in chunks, "
                               "e.g. {\"line_range\": [100, 200]}")
            return result
        else:
            return f"Error: {result}"
