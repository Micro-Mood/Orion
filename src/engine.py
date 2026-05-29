"""
Orion AI 引擎
=============

单阶段原生 Tool Calling 循环 + register_tool 元工具：
  始终可用: register_tool / unregister_tool
  模型调 register_tool(names=[...]) 后，后续轮才能调用该工具
  纯文本回复不结束本轮——必须显式调 done
    已注册工具空闲 N 秒后自动卸载（TTL，可配）
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
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Dict, List, Optional, Tuple

from context import Context, Message
from llm import LLMClient, LLMError
from mcp_client import MCPClient
from prompt import build_system_prompt, build_user_content_with_runtime
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
    # (prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens) — 单次 LLM call 的用量
    on_usage: Optional[Callable[[int, int, int, int], Awaitable[None]]] = None
    # 上下文压缩事件 (类似工具调用): 开始 / 结束
    on_compress_start: Optional[Callable[[int, int], Awaitable[None]]] = None
    # (success, info dict): {title, file, archived, before_tokens}
    on_compress_end: Optional[Callable[[bool, Dict[str, Any]], Awaitable[None]]] = None


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
                 tool_ttl_seconds: int = 300,
                 context_window: int = 128000,
                 compress_at: float = 0.55,
                 context_recent_n: int = 4,
                 memory_dir: str = ".orion"):
        self.llm = llm
        self.mcp = mcp
        self.store = store
        self.max_iterations = max_iterations
        self.read_file_max_lines = read_file_max_lines
        self.tool_ttl_seconds = tool_ttl_seconds
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
                  auto_confirm_dangerous: bool = False,
                  user_msg_id: Optional[str] = None,
                  ai_msg_id: Optional[str] = None,
                  turn_id: Optional[str] = None) -> EngineResult:
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

        if user_content is not None:
            turn_id = turn_id or f"turn_{uuid.uuid4().hex[:8]}"
            user_msg_id = user_msg_id or f"user_{uuid.uuid4().hex[:8]}"
        else:
            turn_id = turn_id or self._infer_active_turn_id(session_id)
        ai_msg_id = ai_msg_id or f"ai_{uuid.uuid4().hex[:8]}"

        def run_meta(extra: Optional[Dict[str, Any]] = None,
                     msg_id: Optional[str] = None) -> Dict[str, Any]:
            meta: Dict[str, Any] = {}
            if turn_id:
                meta["turn_id"] = turn_id
            if msg_id:
                meta["msg_id"] = msg_id
            if extra:
                meta.update(extra)
            return meta

        # 1. 保存用户消息（resume 时 user_content=None，跳过写入）
        if user_content is not None:
            self.store.add_context(
                session_id, "user", build_user_content_with_runtime(user_content),
                metadata=run_meta(msg_id=user_msg_id),
            )

        # 2. 构建上下文（从 store 恢复完整历史; token 压缩在 prepare 阶段触发）
        ctx = Context()
        memory_section = memory_mod.build_memory_section(self.cwd, self.memory_dir)
        ctx.set_system(build_system_prompt(
            self.cwd, self.tool_ttl_seconds, memory_index=memory_section,
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
                                                msg.get("content"),
                                                msg.get("reasoning_content"))
                else:
                    ctx.add_assistant(content, msg.get("reasoning_content"))
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

                # 上下文压缩触发: 调 LLM 前按当前 session 的上下文估算。
                # 不能使用全局 llm.last_usage：它可能来自其他会话或上一轮调用。
                actual_prompt = ctx.token_estimate()
                if iteration == 1:
                    actual_prompt = max(
                        actual_prompt,
                        self._session_token_hint(session_id),
                    )
                if ctx.needs_compression(self.context_window, self.compress_at,
                                          real_prompt_tokens=actual_prompt):
                    try:
                        await self._compress_context(session_id, ctx, callbacks,
                                                      real_prompt_tokens=actual_prompt,
                                                      active_turn_id=turn_id)
                    except Exception as e:
                        logger.error(f"[{session_id}] 压缩上下文失败: {e}",
                                     exc_info=True)

                # 构建本轮可用工具：始终可用 + 已注册
                evicted = ctx.evict_expired(self.tool_ttl_seconds)
                if evicted:
                    persist_registered()
                    logger.debug(f"[{session_id}] evicted expired tools: {evicted}")
                tools = (get_always_available_schemas()
                         + get_schemas_for_registered(
                             list(ctx.registered_tools.keys())))

                # 单次 LLM 调用（流式）
                full_text, full_reasoning, tool_calls, model = await self._stream_round(
                    ctx, tools, callbacks
                )
                last_model = model

                # 没有 tool_calls：纯文本回复 = 本轮结束
                if not tool_calls:
                    if full_text:
                        ctx.add_assistant(full_text, full_reasoning or None)
                        entry = {
                            "role": "assistant",
                            "content": full_text,
                            "metadata": run_meta({"text_only": True}, ai_msg_id),
                        }
                        if full_reasoning:
                            entry["reasoning_content"] = full_reasoning
                        self.store.add_context_entry(session_id, entry)
                    persist_registered()
                    return EngineResult(
                        full_text or "", tool_records,
                        model=last_model,
                        is_error=not full_text,
                    )

                # 写入 assistant tool_calls
                ctx.add_tool_call_assistant(tool_calls, full_text or None,
                                            full_reasoning or None)
                assistant_entry = {
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": tool_calls,
                    "metadata": run_meta({"iter": iteration}, ai_msg_id),
                }
                if full_reasoning:
                    assistant_entry["reasoning_content"] = full_reasoning
                self.store.add_context_entry(session_id, assistant_entry)

                early_return: Optional[EngineResult] = None

                for tc_index, tc in enumerate(tool_calls):
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
                            "metadata": run_meta({"success": True}, ai_msg_id),
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
                            "metadata": run_meta({"success": True}, ai_msg_id),
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
                            "metadata": run_meta({"success": False}, ai_msg_id),
                        })
                        consecutive_failures += 1
                        continue

                    # ---- ctrl: ask / fail / set_session_title ----
                    if name == "ask":
                        question = args.get("question", "")
                        if question:
                            await self._emit_text(callbacks, question)
                        raw_opts = args.get("options", [])
                        options = ([str(o) for o in raw_opts]
                                   if isinstance(raw_opts, list) else [])
                        ctx.add_tool_result(tc_id, name, "ok")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": "ok",
                            "metadata": run_meta({"success": True}, ai_msg_id),
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
                        ctx.add_tool_result(tc_id, name, "ok")
                        self.store.add_context_entry(session_id, {
                            "role": "tool", "tool_call_id": tc_id,
                            "name": name, "content": "ok",
                            "metadata": run_meta({"success": True}, ai_msg_id),
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
                            "metadata": run_meta({"success": True}, ai_msg_id),
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
                            "metadata": run_meta({"success": False}, ai_msg_id),
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
                            pending_tool_calls=tool_calls[tc_index:],
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
                            **run_meta(msg_id=ai_msg_id),
                        },
                    })
                    ctx.touch_tool(name)

                    if consecutive_failures >= 3:
                        note = ("Multiple consecutive tool failures. "
                                "Please check parameters or try a different approach.")
                        ctx.add_system_note(note)
                        self.store.add_context(
                            session_id, "system", note,
                            metadata=run_meta({"type": "system_inject"}, ai_msg_id)
                        )
                        consecutive_failures = 0
                        break

                if early_return is not None:
                    # done/ask/fail/cancel/pending_confirm 退出前先持久化注册表
                    persist_registered()
                    return early_return

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

    def _infer_active_turn_id(self, session_id: str) -> Optional[str]:
        for entry in reversed(self.store.get_context(session_id)):
            meta = entry.get("metadata") or {}
            turn_id = meta.get("turn_id")
            if turn_id:
                return str(turn_id)
        return None

    def _session_token_hint(self, session_id: str) -> int:
        session = self.store.get_session(session_id) or {}
        total = 0
        for key in ("last_prompt_tokens", "last_completion_tokens"):
            try:
                total += int(session.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return total

    @staticmethod
    def _entry_token_estimate(entry: Dict[str, Any]) -> int:
        total = 0
        content = entry.get("content")
        if isinstance(content, str):
            total += len(content)
        reasoning = entry.get("reasoning_content")
        if isinstance(reasoning, str):
            total += len(reasoning)
        if entry.get("tool_calls"):
            try:
                total += len(json.dumps(entry["tool_calls"], ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        return max(1, total // 3)

    @classmethod
    def _entries_token_estimate(cls, entries: List[Dict[str, Any]]) -> int:
        return sum(cls._entry_token_estimate(e) for e in entries)

    @staticmethod
    def _entry_to_message(entry: Dict[str, Any]) -> Optional[Message]:
        role = entry.get("role", "")
        content = entry.get("content") or ""
        if role == "assistant" and entry.get("tool_calls"):
            return Message(role="assistant", content=entry.get("content"),
                           reasoning_content=entry.get("reasoning_content"),
                           tool_calls=entry.get("tool_calls"))
        if role == "tool":
            return Message(role="tool", content=content,
                           tool_call_id=entry.get("tool_call_id", ""),
                           name=entry.get("name", ""))
        if role in ("user", "assistant", "system"):
            return Message(role=role, content=content,
                           reasoning_content=entry.get("reasoning_content"))
        return None

    def _sync_ctx_from_raw(self, ctx: Context, entries: List[Dict[str, Any]]) -> None:
        ctx.history = []
        for entry in entries:
            msg = self._entry_to_message(entry)
            if msg:
                ctx.history.append(msg)

    @staticmethod
    def _split_turns(entries: List[Dict[str, Any]]) -> Tuple[List[Dict], List[Dict]]:
        prelude: List[Dict] = []
        turns: List[Dict] = []
        current: Optional[Dict[str, Any]] = None

        for idx, entry in enumerate(entries):
            if entry.get("role") == "user":
                if current:
                    turns.append(current)
                meta = entry.get("metadata") or {}
                current = {
                    "turn_id": str(meta.get("turn_id") or f"legacy_turn_{idx}"),
                    "entries": [entry],
                }
                continue
            if current is not None:
                current["entries"].append(entry)
            else:
                prelude.append(entry)

        if current:
            turns.append(current)
        return prelude, turns

    @staticmethod
    def _flatten_turns(turns: List[Dict]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for turn in turns:
            out.extend(turn.get("entries") or [])
        return out

    @staticmethod
    def _collect_scope_values(entries: List[Dict[str, Any]], key: str,
                              covered_key: str) -> List[str]:
        seen = set()
        values: List[str] = []
        for entry in entries:
            meta = entry.get("metadata") or {}
            covered = meta.get(covered_key) or []
            if isinstance(covered, list):
                for item in covered:
                    if item and item not in seen:
                        seen.add(item)
                        values.append(str(item))
            value = meta.get(key)
            if value and value not in seen:
                seen.add(value)
                values.append(str(value))
        return values

    @classmethod
    def _archive_entry_count(cls, entries: List[Dict[str, Any]]) -> int:
        total = 0
        for entry in entries:
            meta = entry.get("metadata") or {}
            if meta.get("type") == "memory_archive":
                try:
                    total += int(meta.get("archived") or 1)
                except (TypeError, ValueError):
                    total += 1
            else:
                total += 1
        return total

    @classmethod
    def _archive_token_count(cls, entries: List[Dict[str, Any]]) -> int:
        total = 0
        for entry in entries:
            meta = entry.get("metadata") or {}
            if meta.get("type") == "memory_archive":
                try:
                    archived_tokens = int(meta.get("archived_tokens") or 0)
                except (TypeError, ValueError):
                    archived_tokens = 0
                total += archived_tokens or cls._entry_token_estimate(entry)
            else:
                total += cls._entry_token_estimate(entry)
        return total or cls._entries_token_estimate(entries)

    def _select_compression_scope(self, raw_ctx: List[Dict[str, Any]],
                                  active_turn_id: Optional[str]) -> Optional[Dict[str, Any]]:
        prelude, turns = self._split_turns(raw_ctx)
        if not turns:
            return None

        active_idx = len(turns) - 1
        if active_turn_id:
            found_active = False
            for i, turn in enumerate(turns):
                if turn.get("turn_id") == active_turn_id:
                    active_idx = i
                    found_active = True
                    break
            if not found_active:
                logger.warning(
                    "压缩时未找到 active_turn_id=%s，使用最后一个 turn 作为保护范围",
                    active_turn_id,
                )

        completed = turns[:active_idx]
        protected = turns[active_idx:]
        archive_prelude = [
            entry for entry in prelude
            if (entry.get("metadata") or {}).get("type") == "memory_archive"
        ]
        keep_prelude = [
            entry for entry in prelude
            if (entry.get("metadata") or {}).get("type") != "memory_archive"
        ]
        if not completed and len(archive_prelude) <= 1:
            return None

        protected_entries = keep_prelude + self._flatten_turns(protected)
        protected_tokens = self._entries_token_estimate(protected_entries)
        handoff_reserve = max(3000, min(10000, int(self.context_window * 0.12)))
        target_after = int(self.context_window * min(0.65, max(0.35, self.compress_at * 0.75)))
        recent_budget = max(0, target_after - protected_tokens - handoff_reserve)

        keep_recent: List[Dict] = []
        kept_tokens = 0
        for turn in reversed(completed):
            if len(keep_recent) >= self.context_recent_n:
                break
            turn_tokens = self._entries_token_estimate(turn.get("entries") or [])
            if kept_tokens + turn_tokens > recent_budget:
                break
            keep_recent.insert(0, turn)
            kept_tokens += turn_tokens

        archive_turns = completed[:len(completed) - len(keep_recent)]
        if not archive_turns and len(archive_prelude) <= 1:
            return None

        to_archive = archive_prelude + self._flatten_turns(archive_turns)
        return {
            "prelude": keep_prelude,
            "archive_turns": archive_turns,
            "keep_recent": keep_recent,
            "protected": protected,
            "to_archive": to_archive,
            "archived_count": self._archive_entry_count(to_archive),
            "archived_tokens": self._archive_token_count(to_archive),
            "covered_msg_ids": self._collect_scope_values(
                to_archive, "msg_id", "covered_msg_ids"),
            "covered_turn_ids": self._collect_scope_values(
                to_archive, "turn_id", "covered_turn_ids"),
        }

    def _serialize_entries_for_archive(self, entries: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        prelude, turns = self._split_turns(entries)
        if not turns and not prelude:
            turns = [{"turn_id": "archive", "entries": entries}]

        def append_entry(entry: Dict[str, Any]) -> None:
            role = entry.get("role", "")
            meta = entry.get("metadata") or {}
            content = entry.get("content") or ""
            if isinstance(content, str) and len(content) > 6000:
                content = content[:6000] + "\n...[此条内容过长，已截断给压缩模型；完整机器副本见 sidecar]"

            if role == "assistant" and entry.get("tool_calls"):
                try:
                    brief_calls = []
                    for tc in entry.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        args = fn.get("arguments", "") or ""
                        if len(args) > 1000:
                            args = args[:1000] + "...[truncated]"
                        brief_calls.append({"name": fn.get("name"), "args": args})
                    calls = json.dumps(brief_calls, ensure_ascii=False)
                except Exception:
                    calls = "[tool_calls]"
                lines.append(f"[assistant tool_calls msg={meta.get('msg_id', '')}]: {calls}")
                if content:
                    lines.append(f"[assistant text]: {content}")
            elif role == "tool":
                lines.append(f"[tool:{entry.get('name', '')} msg={meta.get('msg_id', '')}]: {content}")
            else:
                lines.append(f"[{role} msg={meta.get('msg_id', '')}]: {content}")

        if prelude:
            lines.append("\n### 已有归档交接")
            for entry in prelude:
                append_entry(entry)

        for idx, turn in enumerate(turns, 1):
            lines.append(f"\n### Turn {idx} ({turn.get('turn_id')})")
            for entry in turn.get("entries") or []:
                append_entry(entry)

        history_text = "\n".join(lines).strip()
        max_chars = max(60000, min(self.context_window * 3, 240000))
        if len(history_text) > max_chars:
            history_text = history_text[:max_chars] + "\n...[压缩输入过长，后续原始事件已截断；完整机器副本见 sidecar]"
        return history_text

    @staticmethod
    def _strip_outer_fence(raw: str) -> str:
        text = (raw or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return text

    @staticmethod
    def _tagged_section(text: str, tag: str) -> str:
        start = f"<{tag}>"
        end = f"</{tag}>"
        if start not in text or end not in text:
            return ""
        return text.split(start, 1)[1].split(end, 1)[0].strip()

    def _parse_compression_output(self, raw: str) -> Tuple[str, str]:
        text = self._strip_outer_fence(raw)
        archive_md = self._tagged_section(text, "ORION_ARCHIVE_MD")
        handoff = self._tagged_section(text, "ORION_HANDOFF")
        if not archive_md:
            archive_md = text.strip()
        max_handoff_chars = 6000
        if not handoff:
            handoff = archive_md[:max_handoff_chars]
            if len(archive_md) > max_handoff_chars:
                handoff += "\n...[交接文本由归档正文截断生成，完整内容见归档文件]"
        elif len(handoff) > max_handoff_chars:
            handoff = (
                handoff[:max_handoff_chars]
                + "\n...[交接文本过长已截断，完整内容见归档文件]"
            )
        return archive_md.strip(), handoff.strip()

    @staticmethod
    def _derive_archive_title(markdown: str) -> str:
        for line in (markdown or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()[:30] or "历史归档"
        return "历史归档"

    async def _compress_context(self, session_id: str, ctx: Context,
                                 callbacks: Optional[EngineCallbacks] = None,
                                 real_prompt_tokens: int = 0,
                                 active_turn_id: Optional[str] = None):
        """触发上下文压缩: 按完整 turn 归档旧历史并注入接续交接文本。

        触发时机由调用方判断 (ctx.needs_compression)。本方法不返回值,
        失败时抛异常由调用方捕获。
        """
        raw_ctx = self.store.get_context(session_id)
        scope = self._select_compression_scope(raw_ctx, active_turn_id)
        if not scope:
            return

        to_archive: List[Dict[str, Any]] = scope["to_archive"]
        archived_count = scope["archived_count"]
        archived_tokens = scope["archived_tokens"]

        # 通知前端: 压缩开始 (传待归档条数 + 归档 token 估算)
        if callbacks and callbacks.on_compress_start:
            try:
                await callbacks.on_compress_start(archived_count, archived_tokens)
            except Exception:
                pass

        history_text = self._serialize_entries_for_archive(to_archive)

        sys_prompt = (
            "你是 Orion 的历史归档助手。Orion 是通用 AI agent, "
            "不是只写代码的 agent。请把输入的旧对话压缩成两个 Markdown 产物，"
            "并确保内容适用于各种任务领域。\n\n"
            "输出格式必须严格使用以下两个标签, 不要输出 JSON, 不要包裹代码块:\n"
            "<ORION_ARCHIVE_MD>\n"
            "# 历史归档: <简短标题>\n"
            "...详细归档 Markdown...\n"
            "</ORION_ARCHIVE_MD>\n"
            "<ORION_HANDOFF>\n"
            "...给下一次 LLM 调用的接续交接文本...\n"
            "</ORION_HANDOFF>\n\n"
            "详细归档会写入文件, 不会直接注入后续上下文; 它必须面向人类阅读, "
            "数千到约一万 token 都可接受, 不要压成一两句总结。Orion 是通用工作台, "
            "不要默认按代码项目组织归档; 请按实际任务领域组织, 例如个人计划、日记、资料整理、"
            "研究、写作、运营、数据处理、系统操作、编程等。请尽量保留可追溯细节, 包括: "
            "用户目标与需求变化、按时间顺序的关键过程、重要判断与取舍、涉及的资料/文件/页面/"
            "数据源/配置/账户/外部对象、工具/命令/网页/API 调用及其关键结果、错误信息和修复尝试、"
            "已确认事实、用户明确偏好或原话、当前状态、仍未完成的问题、下一步建议和踩过的坑。"
            "如果确实涉及代码, 再记录相关文件/函数/接口/部署/验证状态; 没有出现的信息不要编造, "
            "合适的地方可以加入评论，对于不确定处要标注。"
            "接续交接文本会被直接注入后续 LLM 上下文, 必须短而高密度, 建议 1000-2000 token, "
            "可以有当前用户意图、已完成/未完成、关键事实、下一步、必须遵守的要求。"
        )
        user_prompt = (
            f"被归档范围: {archived_count} 条 context, 约 {archived_tokens} tokens。\n"
            f"压缩发生在下一次 LLM 调用前；当前用户的新输入不在本归档范围内。\n\n"
            f"旧对话事件流:\n\n{history_text}"
        )

        try:
            resp = await self.llm.chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.2,
            )
            raw = (resp.content or "").strip()
        except LLMError as e:
            logger.warning(f"[{session_id}] 压缩 LLM 调用失败: {e}")
            if callbacks and callbacks.on_compress_end:
                try:
                    await callbacks.on_compress_end(False, {"error": str(e)})
                except Exception:
                    pass
            return

        archive_md, handoff = self._parse_compression_output(raw)
        title = self._derive_archive_title(archive_md)

        # 写记忆文件 + 更新索引
        try:
            rel, sidecar_rel = memory_mod.archive_markdown(
                self.cwd, title, archive_md, dir_name=self.memory_dir,
                sidecar={
                    "session_id": session_id,
                    "archived_count": archived_count,
                    "archived_tokens": archived_tokens,
                    "covered_msg_ids": scope["covered_msg_ids"],
                    "covered_turn_ids": scope["covered_turn_ids"],
                    "entries": to_archive,
                },
                index_extra={
                    "archived_count": archived_count,
                    "archived_tokens": archived_tokens,
                },
            )
        except Exception as e:
            logger.error(f"[{session_id}] 写入记忆失败: {e}")
            if callbacks and callbacks.on_compress_end:
                try:
                    await callbacks.on_compress_end(False, {"error": str(e)})
                except Exception:
                    pass
            return

        note_text = (
            f"[已压缩历史交接]\n"
            f"归档文件: `{rel}`\n"
            f"归档范围: {archived_count} 条 context, 约 {archived_tokens} tokens。\n"
            f"如需早期逐字细节, 先 read_file 加载该归档文件, 不要凭交接文本猜。\n\n"
            f"{handoff}"
        )

        kept_raw = (
            scope["prelude"]
            + [{
                "role": "system",
                "content": note_text,
                "metadata": {
                    "type": "memory_archive",
                    "file": rel,
                    "sidecar": sidecar_rel or "",
                    "archived": archived_count,
                    "archived_tokens": archived_tokens,
                    "covered_msg_ids": scope["covered_msg_ids"],
                    "covered_turn_ids": scope["covered_turn_ids"],
                },
            }]
            + self._flatten_turns(scope["keep_recent"])
            + self._flatten_turns(scope["protected"])
        )
        new_entries = [{
            **entry
        } for entry in kept_raw]
        self.store.set_context(session_id, new_entries)
        self._sync_ctx_from_raw(ctx, new_entries)

        # 重新加载 system_msg 以包含最新索引
        memory_section = memory_mod.build_memory_section(self.cwd, self.memory_dir)
        ctx.set_system(build_system_prompt(
            self.cwd, self.tool_ttl_seconds, memory_index=memory_section,
        ))

        logger.info(
            f"[{session_id}] 上下文已压缩: archived={archived_count} tokens={archived_tokens} → {rel}"
        )

        if callbacks and callbacks.on_compress_end:
            try:
                await callbacks.on_compress_end(True, {
                    "title": title,
                    "file": rel,
                    "archived": archived_count,
                    "archived_tokens": archived_tokens,
                })
            except Exception:
                pass

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

    def _reasoning_history_required(self) -> bool:
        """当前常用 reasoning 模型要求历史 assistant tool_calls 回传 reasoning_content。"""
        model = (self.llm.current_model or "").lower()
        return any(key in model for key in ("deepseek", "qwen", "qwq"))

    def _sanitize_api_messages(self, messages: List[Dict]) -> List[Dict]:
        """发送 API 前清理旧损坏上下文，避免供应商 400。"""
        sanitized: List[Dict] = []
        i = 0
        reasoning_required = self._reasoning_history_required()

        while i < len(messages):
            msg = messages[i]
            role = msg.get("role")

            if role == "tool":
                logger.warning("丢弃孤立 tool message: index=%s name=%s", i, msg.get("name"))
                i += 1
                continue

            if role == "assistant" and msg.get("tool_calls"):
                expected = [
                    str(tc.get("id"))
                    for tc in (msg.get("tool_calls") or [])
                    if tc.get("id")
                ]
                if not expected:
                    logger.warning("丢弃无 id 的 assistant tool_calls: index=%s", i)
                    i += 1
                    continue

                if reasoning_required and not msg.get("reasoning_content"):
                    logger.warning("丢弃缺 reasoning_content 的旧 assistant tool_calls: index=%s", i)
                    i += 1
                    while i < len(messages) and messages[i].get("role") == "tool":
                        i += 1
                    continue

                block = [msg]
                seen = set()
                j = i + 1
                expected_set = set(expected)
                while j < len(messages) and messages[j].get("role") == "tool":
                    tool_call_id = str(messages[j].get("tool_call_id") or "")
                    if tool_call_id not in expected_set or tool_call_id in seen:
                        break
                    block.append(messages[j])
                    seen.add(tool_call_id)
                    j += 1
                    if len(seen) == len(expected_set):
                        break

                if len(seen) == len(expected_set):
                    sanitized.extend(block)
                    i = j
                else:
                    logger.warning(
                        "丢弃不完整 assistant tool_calls 组: index=%s expected=%s seen=%s",
                        i, sorted(expected_set), sorted(seen),
                    )
                    i += 1
                    while i < len(messages) and messages[i].get("role") == "tool":
                        i += 1
                continue

            sanitized.append(msg)
            i += 1

        return sanitized

    async def _stream_round(
        self,
        ctx: Context,
        schemas: List[Dict],
        callbacks: EngineCallbacks,
    ) -> Tuple[str, str, Optional[List[Dict]], str]:
        """单轮流式 LLM 调用。

        tool_choice="auto"：
        - 纯文本回复 → 实时推送给用户，返回 (text, reasoning, None, model)
        - tool_calls → 返回 (text, reasoning, tool_calls, model)
        """
        messages = self._sanitize_api_messages(ctx.build_messages())
        full_text = ""
        full_reasoning = ""
        tool_calls: Optional[List[Dict]] = None
        model = ""

        try:
            async for chunk in self.llm.chat_stream(
                messages, tools=schemas, tool_choice="auto"
            ):
                model = chunk.model

                if chunk.reasoning:
                    full_reasoning += chunk.reasoning
                    if callbacks.on_thinking:
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
                full_reasoning = response.reasoning or ""
                tool_calls = response.tool_calls
                model = response.model

        if callbacks.on_model_info and model:
            try:
                await callbacks.on_model_info(model)
            except Exception:
                pass

        if callbacks.on_usage and self.llm.last_usage:
            try:
                await callbacks.on_usage(
                    self.llm.last_usage.prompt_tokens,
                    self.llm.last_usage.completion_tokens,
                    self.llm.last_usage.total_tokens,
                    self.llm.last_usage.cached_tokens,
                )
            except Exception:
                pass

        return full_text, full_reasoning, tool_calls, model

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

        public_params = dict(params)
        exec_params = dict(params)

        # notion_* 工具：由 Orion 注入 api_key，不暴露给 LLM
        if name.startswith("notion_"):
            from config import get_config  # noqa: PLC0415
            _notion_key = get_config().integrations.notion_api_key
            if _notion_key:
                exec_params["api_key"] = _notion_key

        if callbacks.on_tool_start:
            try:
                await callbacks.on_tool_start(name, public_params)
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
                record = ToolCallRecord(name=name, params=public_params,
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

        mcp_result = await self.mcp.call(name, exec_params)
        duration_ms = int((time.perf_counter() - t0) * 1000)

        if mcp_result.success:
            result_str = json.dumps(
                mcp_result.data or {}, ensure_ascii=False, indent=2
            )
        else:
            result_str = mcp_result.error or "未知错误"

        record = ToolCallRecord(
            name=name, params=public_params,
            success=mcp_result.success,
            result=result_str,
            duration_ms=duration_ms,
        )

        if callbacks.on_tool_end:
            try:
                result_data = {
                    "success": mcp_result.success,
                    "data": result_str if mcp_result.success else None,
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
