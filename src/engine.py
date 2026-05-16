"""
Orion AI 引擎
=============

两阶段原生 Tool Calling 循环:
  SELECT: 用精简 schema 让模型选择工具（ctrl 工具含完整 schema，可直接调用）
  PARAMS: 用完整 schema 让模型填写参数
  EXEC:   执行 tool_calls，结果以 role=tool 写入上下文，循环

特性:
- 原生 OpenAI tool_calls 协议，无自定义 JSON 解析
- SELECT 阶段流式输出（纯文本回复时实时推送）
- ctrl 工具: ask / fail / set_session_title（在 SELECT 直接触发）
- 纯文本回复 = 本轮结束（不需要显式 done 调用）
- 取消操作
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Dict, List, Optional, Tuple

from context import Context, Phase
from llm import LLMClient, LLMError, LLMResponse
from mcp_client import MCPClient
from prompt import build_system_prompt
from store import SessionStore
from tools import TOOLS, get_all_schemas_for_select, get_schemas_for

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
                 max_history: int = 20, max_iterations: int = 30,
                 working_directory: str = "",
                 read_file_max_lines: int = 200):
        self.llm = llm
        self.mcp = mcp
        self.store = store
        self.max_history = max_history
        self.max_iterations = max_iterations
        self.read_file_max_lines = read_file_max_lines
        self.cwd = working_directory or "."

        # 取消标记: session_id → bool
        self._cancel_flags: Dict[str, bool] = {}

    def cancel(self, session_id: str):
        """取消指定会话的处理"""
        self._cancel_flags[session_id] = True

    async def run(self, session_id: str, user_content: str,
                  callbacks: EngineCallbacks) -> EngineResult:
        """
        处理一条用户消息

        引擎全权管理上下文持久化:
        1. 保存用户消息到 store.context[]
        2. 从 store.context[] 恢复完整历史到 Context
        3. 运行 SELECT/PARAMS/EXEC 循环, 每步都持久化
        4. 返回 EngineResult
        """
        self._cancel_flags[session_id] = False

        # 1. 保存用户消息
        self.store.add_context(session_id, "user", user_content)

        # 2. 构建上下文（从 store 恢复完整历史）
        ctx = Context(max_history=self.max_history)
        ctx.set_system(build_system_prompt(self.cwd))

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

        # 3. 确保 MCP 连接
        await self._ensure_mcp()

        # SELECT 阶段使用的 schema（全量，ctrl 带完整参数，axon 精简）
        select_schemas = get_all_schemas_for_select()

        tool_records: List[ToolCallRecord] = []
        last_model = ""
        iteration = 0
        consecutive_failures = 0

        try:
            while iteration < self.max_iterations:
                if self._cancel_flags.get(session_id, False):
                    return EngineResult("Cancelled", tool_records,
                                        model=last_model, cancelled=True)

                iteration += 1
                logger.debug(f"[{session_id}] 迭代 {iteration}")

                # ==================== SELECT ====================
                # 流式调用，tool_choice="auto"
                # - 纯文本回复 → 本轮结束
                # - tool_calls → 处理 ctrl 或转 PARAMS
                full_text, sel_tool_calls, model = await self._stream_select(
                    ctx, select_schemas, callbacks
                )
                last_model = model

                if not sel_tool_calls:
                    # 纯文本回复：直接结束本轮
                    if full_text:
                        ctx.add_assistant(full_text)
                        self.store.add_context(
                            session_id, "assistant", full_text,
                            metadata={"phase": "select"}
                        )
                    return EngineResult(full_text or "", tool_records,
                                        model=last_model)

                # ── 处理 SELECT 返回的 tool_calls ──
                axon_names: List[str] = []
                early_return: Optional[EngineResult] = None

                for tc in sel_tool_calls:
                    name = tc["function"]["name"]
                    args_str = tc["function"].get("arguments", "") or ""
                    try:
                        args = json.loads(args_str) if args_str.strip() else {}
                    except json.JSONDecodeError:
                        args = {}

                    if name == "done":
                        summary = args.get("summary", "") or full_text or ""
                        if summary:
                            await self._emit_text(callbacks, summary)
                            ctx.add_assistant(summary)
                            self.store.add_context(
                                session_id, "assistant", summary,
                                metadata={"phase": "done"}
                            )
                        early_return = EngineResult(
                            summary, tool_records, model=last_model
                        )
                        break

                    elif name == "ask":
                        question = args.get("question", "")
                        if question:
                            await self._emit_text(callbacks, question)
                            ctx.add_assistant(question)
                            self.store.add_context(
                                session_id, "assistant", question,
                                metadata={"phase": "ask"}
                            )
                        raw_opts = args.get("options", [])
                        options = ([str(o) for o in raw_opts]
                                   if isinstance(raw_opts, list) else [])
                        early_return = EngineResult(
                            question, tool_records, model=last_model,
                            is_ask=True, options=options
                        )
                        break

                    elif name == "fail":
                        reason = args.get("reason", "操作失败")
                        await self._emit_text(callbacks, reason)
                        ctx.add_assistant(reason)
                        self.store.add_context(
                            session_id, "assistant", reason,
                            metadata={"phase": "fail"}
                        )
                        early_return = EngineResult(
                            reason, tool_records, model=last_model,
                            is_error=True
                        )
                        break

                    elif name == "set_session_title":
                        title = args.get("title", "")
                        if title and callbacks.on_title_update:
                            try:
                                await callbacks.on_title_update(title)
                            except Exception:
                                pass
                        # 不 break，继续处理其余 tool_calls

                    elif name in TOOLS and TOOLS[name].category != "ctrl":
                        axon_names.append(name)

                if early_return is not None:
                    return early_return

                if not axon_names:
                    # SELECT 只调了 ctrl 工具（如只 set_session_title）
                    # 把 tool_calls 写入上下文 + 补充 tool 结果，
                    # 否则下一轮模型看到相同上下文会重复调用，导致无限循环
                    ctx.add_tool_call_assistant(sel_tool_calls, full_text or None)
                    for tc in sel_tool_calls:
                        tc_id = tc.get("id") or f"call_{tc['function']['name']}_{iteration}"
                        ctx.add_tool_result(tc_id, tc["function"]["name"], "ok")
                    continue

                # 去重，保持顺序
                ctx.selected_tools = list(dict.fromkeys(axon_names))

                # ==================== PARAMS ====================
                # 非流式，tool_choice="required"，只提供选中工具的完整 schema
                params_schemas = get_schemas_for(ctx.selected_tools)
                params_resp = await self._call_with_tools(
                    ctx, params_schemas, "required", callbacks
                )
                last_model = params_resp.model

                params_tool_calls = params_resp.tool_calls
                if not params_tool_calls:
                    # 模型未返回 tool_calls（异常情况）
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        ctx.reset_phase()
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0

                # 写入上下文
                ctx.add_tool_call_assistant(params_tool_calls,
                                            params_resp.content or None)
                self.store.add_context_entry(session_id, {
                    "role": "assistant",
                    "content": params_resp.content or None,
                    "tool_calls": params_tool_calls,
                    "metadata": {"phase": "params", "iter": iteration},
                })

                # ==================== EXEC ====================
                for tc in params_tool_calls:
                    if self._cancel_flags.get(session_id, False):
                        return EngineResult("Cancelled", tool_records,
                                            model=last_model, cancelled=True)

                    name = tc["function"]["name"]
                    args_str = tc["function"].get("arguments", "") or ""
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {}
                    tc_id = tc.get("id") or f"call_{name}_{iteration}"

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

                    if consecutive_failures >= 3:
                        note = ("Multiple consecutive tool failures. "
                                "Please check parameters or try a different approach.")
                        ctx.add_system_note(note)
                        self.store.add_context(
                            session_id, "system", note,
                            metadata={"type": "system_inject"}
                        )
                        break

                ctx.reset_phase()

            # 达到最大迭代
            logger.warning(f"[{session_id}] 达到最大迭代 {self.max_iterations}")
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

    async def _stream_select(
        self,
        ctx: Context,
        schemas: List[Dict],
        callbacks: EngineCallbacks,
    ) -> Tuple[str, Optional[List[Dict]], str]:
        """
        SELECT 阶段流式调用。

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
                    # 最终 chunk，携带累积的 tool_calls
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
            # 流式失败，降级非流式
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

        return full_text, tool_calls, model

    async def _call_with_tools(
        self,
        ctx: Context,
        tools: List[Dict],
        tool_choice: str,
        callbacks: EngineCallbacks,
    ) -> LLMResponse:
        """非流式 LLM 调用（PARAMS 阶段）"""
        messages = ctx.build_messages()
        response = await self.llm.chat(
            messages, tools=tools, tool_choice=tool_choice
        )
        if callbacks.on_model_info and response.model:
            try:
                await callbacks.on_model_info(response.model)
            except Exception:
                pass
        return response

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
