"""
Orion AI 引擎
=============

两阶段工具调用循环:
  SELECT: AI 从工具名列表中选工具 (省 token)
  PARAMS: 注入选中工具的参数描述, AI 填参数并调用
  EXEC:   执行工具, 结果回注上下文, 循环

特性:
- 流式输出 (SELECT 阶段智能判断 JSON/文本)
- 全量中间消息持久化 (多轮上下文连续)
- 控制指令: done(结束), ask(提问), fail(失败)
- 连续工具失败检测
- PARAMS 阶段溢出保护
- 取消操作
"""

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Dict, List, Optional

from context import Context, Phase
from llm import LLMClient, LLMError, LLMResponse, StreamChunk
from mcp_client import MCPClient, MCPResult
from prompt import build_system_prompt
from store import SessionStore
from tools import get_tool, get_compact_desc, get_names_of_category

logger = logging.getLogger(__name__)


# ==================== 数据结构 ====================

@dataclass
class EngineCallbacks:
    """引擎回调，用于向 WebSocket 推送事件"""
    on_text: Optional[Callable[[str], Awaitable[None]]] = None
    on_tool_start: Optional[Callable[[str, Dict], Awaitable[None]]] = None
    on_tool_end: Optional[Callable[[str, Dict, bool, int], Awaitable[None]]] = None
    on_model_info: Optional[Callable[[str], Awaitable[None]]] = None


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


# ==================== 解析器 ====================

def parse_tool_select(response: str) -> List[str]:
    """
    解析工具选择: {"select": ["tool1", "tool2"]}
    支持分类名展开: {"select": ["file"]} → 展开为 file 分类下的所有工具
    """
    match = re.search(r'\{\s*"select"\s*:\s*\[([^\]]*)\]', response)
    if not match:
        return []

    try:
        names = re.findall(r'"([^"]+)"', match.group(1))
        if not names:
            return []

        expanded = []
        for n in names:
            cat_tools = get_names_of_category(n)
            if cat_tools:
                expanded.extend(cat_tools)
            else:
                expanded.append(n)
        return expanded
    except Exception:
        return []


def parse_tool_call(response: str) -> Optional[Dict[str, Any]]:
    """解析单个工具调用（返回第一个）"""
    calls = parse_all_tool_calls(response)
    return calls[0] if calls else None


def parse_all_tool_calls(response: str) -> List[Dict[str, Any]]:
    """
    解析所有工具调用（按出现顺序）

    支持:
    - ```json {...} ```
    - ``` {...} ```
    - 内联 JSON: {"call": "tool", ...}

    去重: 按 JSON 内容去重（不是位置），防止同一个调用被不同 pattern 匹配两次。
    例如 ```json {"call": "x"} ``` 会被 pattern1 和 pattern3 各匹配一次但位置不同。
    """
    results = []
    seen_keys = set()  # 用 (call, json_str) 去重

    patterns = [
        r'```json\s*([\s\S]*?)```',
        r'```\s*([\s\S]*?)```',
        r'(\{[^{}]*"call"[^{}]*\})',
        r'(\{"call"[\s\S]*?\})',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, response, re.MULTILINE):
            text = m.group(1) if m.lastindex else m.group(0)
            try:
                clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '',
                               text.strip())
                data = json.loads(clean)
                if isinstance(data, dict) and "call" in data:
                    # 按规范化 JSON 去重
                    dedup_key = json.dumps(data, sort_keys=True,
                                           ensure_ascii=False)
                    if dedup_key not in seen_keys:
                        seen_keys.add(dedup_key)
                        results.append(data)
            except (json.JSONDecodeError, ValueError):
                continue

    if not results:
        try:
            clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '',
                           response.strip())
            data = json.loads(clean)
            if isinstance(data, dict) and "call" in data:
                results.append(data)
        except (json.JSONDecodeError, ValueError):
            pass

    return results


# ==================== 引擎 ====================

class OrionEngine:
    """
    Orion AI 引擎

    每条用户消息触发一次 run()。
    引擎直接管理持久化: 所有中间消息写入 store.context[],
    确保多轮上下文连续。
    """

    def __init__(self, llm: LLMClient, mcp: MCPClient, store: SessionStore,
                 max_history: int = 20, max_iterations: int = 30,
                 working_directory: str = ""):
        self.llm = llm
        self.mcp = mcp
        self.store = store
        self.max_history = max_history
        self.max_iterations = max_iterations
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

        Args:
            session_id: 会话 ID
            user_content: 用户消息内容
            callbacks: WebSocket 推送回调

        Returns:
            EngineResult 执行结果
        """
        self._cancel_flags[session_id] = False

        # 1. 保存用户消息到上下文
        self.store.add_context(session_id, "user", user_content)

        # 2. 构建上下文 (从 store 恢复完整历史)
        ctx = Context(max_history=self.max_history)
        ctx.set_system(build_system_prompt(self.cwd))

        all_ctx = self.store.get_context(session_id)
        for msg in all_ctx:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                ctx.add_user(content)
            elif role == "assistant":
                ctx.add_assistant(content)

        # 3. 确保 MCP 连接 + 设置工作目录
        await self._ensure_mcp()

        tool_calls: List[ToolCallRecord] = []
        last_model = ""
        iteration = 0
        consecutive_tool_failures = 0
        params_parse_failures = 0

        try:
            while iteration < self.max_iterations:
                # 检查取消
                if self._cancel_flags.get(session_id, False):
                    return EngineResult("已取消", tool_calls,
                                       model=last_model, cancelled=True)

                iteration += 1
                logger.debug(
                    f"[{session_id}] 迭代 {iteration}, "
                    f"阶段: {ctx.phase.value}"
                )

                # ==================== SELECT 阶段 ====================
                if ctx.phase == Phase.SELECT:
                    full_text, model = await self._stream_select(
                        ctx, callbacks, session_id
                    )
                    last_model = model
                    ctx.add_assistant(full_text)
                    self.store.add_context(
                        session_id, "assistant", full_text,
                        metadata={"phase": "select", "iter": iteration}
                    )

                    # 情况 1: AI 直接调用了工具（跳过 SELECT）
                    call_data = parse_tool_call(full_text)
                    if call_data:
                        ctrl = await self._check_control(
                            call_data, tool_calls, last_model, callbacks
                        )
                        if ctrl is not None:
                            return ctrl

                        tool_name = call_data.get("call", "")
                        tool_def = get_tool(tool_name)

                        if tool_def:
                            ctx.selected_tools = [tool_name]
                            valid_params = {p.name for p in tool_def.params}
                            used_params = {k for k in call_data
                                           if k != "call"}
                            invalid = used_params - valid_params

                            if invalid:
                                desc = get_compact_desc([tool_name])
                                fix_msg = (
                                    f"参数错误: {', '.join(invalid)} 不是 "
                                    f"{tool_name} 的有效参数。\n"
                                    f"工具说明:\n{desc}\n\n"
                                    f"请重新调用。"
                                )
                                ctx.add_user(fix_msg)
                                self.store.add_context(
                                    session_id, "user", fix_msg,
                                    metadata={"type": "system_inject"}
                                )
                                ctx.phase = Phase.PARAMS
                            else:
                                ctx.phase = Phase.EXEC
                            continue
                        else:
                            err = (f"工具 '{tool_name}' 不存在，"
                                   f"请从可用工具中选择。")
                            ctx.add_user(err)
                            self.store.add_context(
                                session_id, "user", err,
                                metadata={"type": "system_inject"}
                            )
                            continue

                    # 情况 2: AI 选择了工具
                    selected = parse_tool_select(full_text)
                    valid = [n for n in selected if get_tool(n)]

                    if valid:
                        ctx.selected_tools = valid
                        ctx.phase = Phase.PARAMS
                        continue

                    # 情况 3: JSON 格式错误
                    has_json = bool(re.search(
                        r'\{[^}]*"(?:select|call)"', full_text
                    ))
                    if has_json:
                        fix_msg = (
                            "JSON 格式有误，请重新输入。\n"
                            "选择工具: {\"select\": [\"工具名\"]}\n"
                            "调用工具: {\"call\": \"工具名\", \"参数\": \"值\"}"
                        )
                        ctx.add_user(fix_msg)
                        self.store.add_context(
                            session_id, "user", fix_msg,
                            metadata={"type": "system_inject"}
                        )
                        continue

                    # 情况 4: 纯文本直接回复
                    # (已在 _stream_select 中流式推送到前端)
                    return EngineResult(
                        full_text, tool_calls, model=last_model
                    )

                # ==================== PARAMS 阶段 ====================
                if ctx.phase == Phase.PARAMS:
                    desc = get_compact_desc(ctx.selected_tools)
                    tool_prompt = (
                        f"工具说明:\n{desc}\n\n"
                        f"请调用工具: "
                        f"{{\"call\": \"工具名\", \"参数名\": \"值\"}}"
                    )
                    ctx.add_user(tool_prompt)
                    self.store.add_context(
                        session_id, "user", tool_prompt,
                        metadata={"type": "system_inject"}
                    )

                    response = await self._call_llm(ctx, callbacks)
                    last_model = response.model
                    ai_text = response.content
                    ctx.add_assistant(ai_text)
                    self.store.add_context(
                        session_id, "assistant", ai_text,
                        metadata={"phase": "params"}
                    )

                    call_data = parse_tool_call(ai_text)
                    if call_data:
                        # 检查控制指令
                        ctrl = await self._check_control(
                            call_data, tool_calls, last_model, callbacks
                        )
                        if ctrl is not None:
                            return ctrl

                        params_parse_failures = 0
                        ctx.phase = Phase.EXEC
                        continue

                    # 解析失败
                    params_parse_failures += 1
                    if params_parse_failures >= 3:
                        escape_msg = (
                            "无法解析工具调用格式。"
                            "请直接用自然语言回答用户的问题，"
                            "不要使用工具。"
                        )
                        ctx.add_user(escape_msg)
                        self.store.add_context(
                            session_id, "user", escape_msg,
                            metadata={"type": "system_inject"}
                        )
                        ctx.reset_phase()
                        continue

                    fix_msg = (
                        "请按格式调用工具: "
                        "{\"call\": \"工具名\", \"参数名\": \"值\"}"
                    )
                    ctx.add_user(fix_msg)
                    self.store.add_context(
                        session_id, "user", fix_msg,
                        metadata={"type": "system_inject"}
                    )
                    continue

                # ==================== EXEC 阶段 ====================
                if ctx.phase == Phase.EXEC:
                    last_msg = ctx.get_last_assistant_msg()
                    all_calls = (parse_all_tool_calls(last_msg)
                                 if last_msg else [])

                    if not all_calls:
                        ctx.reset_phase()
                        continue

                    for call_data in all_calls:
                        # 检查取消
                        if self._cancel_flags.get(session_id, False):
                            return EngineResult(
                                "已取消", tool_calls,
                                model=last_model, cancelled=True
                            )

                        tool_name = call_data.get("call", "")
                        tool_args = {k: v for k, v in call_data.items()
                                     if k != "call"}

                        # 控制指令
                        ctrl = await self._check_control(
                            call_data, tool_calls, last_model, callbacks
                        )
                        if ctrl is not None:
                            return ctrl

                        # 执行工具
                        record = await self._exec_tool(
                            tool_name, tool_args, callbacks
                        )
                        tool_calls.append(record)

                        # 连续工具失败检测
                        if record.success:
                            consecutive_tool_failures = 0
                        else:
                            consecutive_tool_failures += 1
                            if consecutive_tool_failures >= 3:
                                error_msg = (
                                    "连续多次工具调用失败。"
                                    "请检查操作参数是否正确，"
                                    "或换一种方式完成任务。"
                                )
                                ctx.add_user(error_msg)
                                self.store.add_context(
                                    session_id, "user", error_msg,
                                    metadata={"type": "system_inject"}
                                )
                                ctx.reset_phase()
                                break

                        # 结果写入上下文和 store
                        result_text = self._format_result(
                            tool_name, record.success, record.result
                        )
                        ctx.add_user(result_text)
                        self.store.add_context(
                            session_id, "user", result_text,
                            metadata={
                                "type": "tool_result",
                                "tool": tool_name,
                                "success": record.success,
                                "duration_ms": record.duration_ms,
                            }
                        )

                    # 所有调用完成, 回到 SELECT
                    ctx.reset_phase()

            # 达到最大迭代
            logger.warning(
                f"[{session_id}] 达到最大迭代 {self.max_iterations}"
            )
            return EngineResult(
                f"已达到最大步骤数 ({self.max_iterations})，"
                f"请简化请求重试。",
                tool_calls, model=last_model, is_error=True
            )

        except LLMError as e:
            logger.error(f"[{session_id}] LLM 错误: {e}")
            return EngineResult(f"AI 服务错误: {e}", tool_calls,
                               model=last_model, is_error=True)
        except Exception as e:
            logger.error(f"[{session_id}] 引擎异常: {e}", exc_info=True)
            return EngineResult(f"内部错误: {e}", tool_calls,
                               model=last_model, is_error=True)
        finally:
            self._cancel_flags.pop(session_id, None)

    # ==================== 控制指令 ====================

    async def _check_control(self, call_data: Dict,
                             tool_calls: List[ToolCallRecord],
                             model: str,
                             callbacks: EngineCallbacks
                             ) -> Optional[EngineResult]:
        """检查并处理控制指令 (done/ask/fail)，非控制指令返回 None"""
        name = call_data.get("call", "")

        if name == "done":
            summary = call_data.get("summary", "")
            if summary:
                await self._emit_text(callbacks, summary)
            return EngineResult(summary or "完成", tool_calls, model=model)

        if name == "ask":
            question = call_data.get("question", "")
            if question:
                await self._emit_text(callbacks, question)
            raw_opts = call_data.get("options", [])
            options = [str(o) for o in raw_opts] if isinstance(raw_opts, list) else []
            return EngineResult(
                question, tool_calls, model=model, is_ask=True,
                options=options
            )

        if name == "fail":
            reason = call_data.get("reason", "操作失败")
            if reason:
                await self._emit_text(callbacks, reason)
            return EngineResult(
                reason, tool_calls, model=model, is_error=True
            )

        return None

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

    async def _stream_select(self, ctx: Context,
                             callbacks: EngineCallbacks,
                             session_id: str) -> tuple:
        """
        SELECT 阶段带智能流式检测

        如果 AI 回复 JSON → 静默缓冲 (工具选择/调用)
        如果 AI 回复自然语言 → 实时流式推送到前端

        Returns:
            (full_text, model) 元组
        """
        messages = ctx.build_messages()
        full_text = ""
        is_text_reply = None  # None=未决定
        model = ""

        try:
            async for chunk in self.llm.chat_stream(messages):
                model = chunk.model
                full_text += chunk.content

                # 第一个非空字符决定模式
                if is_text_reply is None:
                    stripped = full_text.strip()
                    if len(stripped) > 0:
                        first_char = stripped[0]
                        if first_char in ('{', '`'):
                            is_text_reply = False
                        else:
                            is_text_reply = True
                            if callbacks.on_text:
                                await callbacks.on_text(full_text)
                elif is_text_reply:
                    if callbacks.on_text:
                        await callbacks.on_text(chunk.content)

        except LLMError:
            # 流式失败, 降级到非流式
            if not full_text:
                response = await self.llm.chat(messages)
                full_text = response.content
                model = response.model
                # 判断是否为纯文本
                stripped = full_text.strip()
                if stripped and stripped[0] not in ('{', '`'):
                    if callbacks.on_text:
                        await callbacks.on_text(full_text)

        # 广播模型信息
        if callbacks.on_model_info and model:
            try:
                await callbacks.on_model_info(model)
            except Exception:
                pass

        return full_text, model

    async def _call_llm(self, ctx: Context,
                        callbacks: EngineCallbacks) -> LLMResponse:
        """非流式 LLM 调用 (PARAMS 等确定需要 JSON 回复的阶段)"""
        messages = ctx.build_messages()
        response = await self.llm.chat(messages)

        if callbacks.on_model_info:
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

        # 广播开始
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
                result_text = f"{name} 失败: Axon MCP Server 未连接"
                record = ToolCallRecord(
                    name=name, params=params, success=False,
                    result=result_text, duration_ms=duration_ms
                )
                if callbacks.on_tool_end:
                    try:
                        await callbacks.on_tool_end(
                            name, {"error": result_text}, False, duration_ms
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
            name=name, params=params, success=mcp_result.success,
            result=result_str, duration_ms=duration_ms
        )

        # 广播结束
        if callbacks.on_tool_end:
            try:
                result_data = {
                    "success": mcp_result.success,
                    "data": (result_str[:500]
                             if mcp_result.success else None),
                    "error": (mcp_result.error
                              if not mcp_result.success else None),
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

    def _format_result(self, tool: str, success: bool,
                       result: str) -> str:
        """格式化工具执行结果（给 AI 看的）"""
        if success:
            max_len = 6000 if tool == "read_file" else 1500
            display = result
            if len(result) > max_len:
                display = result[:max_len]
                display += f"\n...(已截断，原始 {len(result)} 字符)"
                if tool == "read_file":
                    display += (
                        "\n提示: 用 line_range 参数分段读取，"
                        "如: {\"call\": \"read_file\", \"path\": \"...\","
                        " \"line_range\": [100, 200]}"
                    )
            return f"[{tool}] 成功:\n{display}"
        else:
            return f"[{tool}] 失败: {result}"
