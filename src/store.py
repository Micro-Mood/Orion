"""
Orion 会话持久化
================

JSON 文件存储，会话与消息分离。
- data/sessions.json: 会话元数据列表
- data/messages/{session_id}.json: 每个会话的消息
  - messages[]: 前端展示的消息 (用户可见)
  - context[]: AI 引擎上下文消息 (包含中间推理、工具注入、执行结果)

支持原子写入（跨平台）和自动截断。
"""

import json
import os
import uuid
import time
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# session_id 格式: 8位十六进制 (uuid4 前8位)
_VALID_SID = re.compile(r'^[a-f0-9]{8}$')

# 数据目录: Orion/data/
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 限制常量
MAX_MESSAGES_PER_SESSION = 500
MAX_CONTEXT_PER_SESSION = 200
MAX_MESSAGE_SIZE_BYTES = 200 * 1024  # 200KB，允许较长的压缩交接文本
MAX_HISTORY_FILE_SIZE_MB = 5


class SessionStore:
    """
    会话存储

    线程安全（RLock），原子写入防止数据损坏。
    消息和上下文分离: messages[] 给前端, context[] 给 AI 引擎。
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.sessions_file = self.data_dir / "sessions.json"
        self.messages_dir = self.data_dir / "messages"
        self._lock = threading.RLock()

        # 确保目录存在
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.messages_dir.mkdir(parents=True, exist_ok=True)

        # 初始化会话文件
        if not self.sessions_file.exists():
            self._save_sessions_raw({"sessions": []})

    # ==================== 会话 CRUD ====================

    def create_session(self, session_id: str, title: str = "新对话") -> Dict:
        """创建会话"""
        now = datetime.now(timezone.utc).isoformat()
        session = {
            "id": session_id,
            "title": title,
            "tokens": 0,
            "last_prompt_tokens": 0,
            "last_completion_tokens": 0,
            "last_msg_tokens": 0,
            "created_at": now,
            "updated_at": now,
        }

        with self._lock:
            data = self._load_sessions_raw()
            data["sessions"].append(session)
            self._save_sessions_raw(data)

        self._init_messages(session_id)
        return session

    def get_session(self, session_id: str) -> Optional[Dict]:
        """获取会话"""
        if not _VALID_SID.match(session_id):
            return None
        data = self._load_sessions_raw()
        for s in data["sessions"]:
            if s["id"] == session_id:
                return s
        return None

    def update_session(self, session_id: str, **kwargs) -> bool:
        """更新会话字段"""
        with self._lock:
            data = self._load_sessions_raw()
            for s in data["sessions"]:
                if s["id"] == session_id:
                    for key, value in kwargs.items():
                        s[key] = value
                    s["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self._save_sessions_raw(data)
                    return True
            return False

    def get_session_registered_tools(self, session_id: str) -> Dict[str, float]:
        """获取会话级已注册工具表 {name: last_used_ts}。"""
        s = self.get_session(session_id)
        if not s:
            return {}
        data = s.get("registered_tools") or {}
        if not isinstance(data, dict):
            return {}
        # 旧版本存的是 idle_rounds 小整数；迁移时当作“刚使用过”，避免升级后立即卸载。
        now = time.time()
        out: Dict[str, float] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, (int, float)):
                continue
            ts = float(v)
            out[k] = ts if ts >= 1000000000 else now
        return out

    def set_session_registered_tools(self, session_id: str,
                                     data: Dict[str, float]) -> bool:
        """持久化会话级已注册工具表。"""
        return self.update_session(session_id, registered_tools=dict(data))

    def update_session_tokens(self, session_id: str, delta: int,
                              last_prompt_tokens: Optional[int] = None,
                              last_completion_tokens: Optional[int] = None,
                              last_msg_tokens: Optional[int] = None) -> bool:
        """累加会话 tokens（持久化）。可同时刷新：
        - last_prompt_tokens / last_completion_tokens: 最后一次 LLM 调用的用量（用于算 ctx 大小）
        - last_msg_tokens: 本轮消息总花费（多次调用累加）
        """
        with self._lock:
            data = self._load_sessions_raw()
            for s in data["sessions"]:
                if s["id"] == session_id:
                    s["tokens"] = s.get("tokens", 0) + delta
                    if last_prompt_tokens is not None and last_prompt_tokens > 0:
                        s["last_prompt_tokens"] = int(last_prompt_tokens)
                    if last_completion_tokens is not None and last_completion_tokens >= 0:
                        s["last_completion_tokens"] = int(last_completion_tokens)
                    if last_msg_tokens is not None and last_msg_tokens > 0:
                        s["last_msg_tokens"] = int(last_msg_tokens)
                    s["updated_at"] = datetime.now(timezone.utc).isoformat()
                    self._save_sessions_raw(data)
                    return True
            return False

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其消息"""
        if not _VALID_SID.match(session_id):
            return False
        with self._lock:
            data = self._load_sessions_raw()
            original_len = len(data["sessions"])
            data["sessions"] = [s for s in data["sessions"]
                                if s["id"] != session_id]

            if len(data["sessions"]) < original_len:
                self._save_sessions_raw(data)
                msg_file = self.messages_dir / f"{session_id}.json"
                if msg_file.exists():
                    msg_file.unlink()
                return True
            return False

    def list_sessions(self) -> List[Dict]:
        """获取所有会话（按更新时间倒序）"""
        data = self._load_sessions_raw()
        sessions = data.get("sessions", [])
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions

    def fork_session(self, src_id: str, anchor_msg_id: str,
                     title: str = "分叉对话",
                     cwd: Optional[str] = None,
                     memory_dir: str = ".orion") -> Optional[Dict]:
        """从指定消息处截断创建新会话。

        新数据优先使用 context.metadata.msg_id/turn_id 精确切分；压缩归档
        note 通过 covered_msg_ids 判断是否属于 fork 范围。旧数据没有这些
        元信息时才退回内容/时间戳启发式。
        """
        src_msgs = self.get_messages(src_id)
        src_ctx = self.get_context(src_id)

        anchor_idx = None
        for i, m in enumerate(src_msgs):
            if m.get("id") == anchor_msg_id:
                anchor_idx = i
                break
        if anchor_idx is None:
            return None

        msgs_slice = src_msgs[:anchor_idx + 1]
        anchor_msg = src_msgs[anchor_idx]
        anchor_ts = anchor_msg.get("timestamp", "")

        allowed_msg_ids = {m.get("id") for m in msgs_slice if m.get("id")}

        seen_sidecars = set()

        def load_sidecar_entries(sidecar_rel: str) -> List[Dict]:
            if not cwd or not sidecar_rel:
                return []
            try:
                rel = sidecar_rel.replace("\\", "/").lstrip("/")
                if rel in seen_sidecars:
                    return []
                seen_sidecars.add(rel)
                p = Path(cwd) / rel
                data = json.loads(p.read_text(encoding="utf-8"))
                entries = data.get("entries", [])
                return entries if isinstance(entries, list) else []
            except (OSError, json.JSONDecodeError, ValueError):
                return []

        def entry_msg_id(entry: Dict) -> str:
            meta = entry.get("metadata") or {}
            mid = meta.get("msg_id") or entry.get("msg_id")
            return str(mid) if mid else ""

        def entry_covered_ids(entry: Dict) -> set:
            meta = entry.get("metadata") or {}
            raw = meta.get("covered_msg_ids") or []
            return {str(x) for x in raw if x}

        has_linked_ctx = any(
            entry_msg_id(c) or entry_covered_ids(c)
            for c in src_ctx
        )

        def build_linked_context() -> List[Dict]:
            out: List[Dict] = []

            def append_entry(entry: Dict):
                meta = entry.get("metadata") or {}
                if meta.get("type") == "memory_archive":
                    covered = entry_covered_ids(entry)
                    if covered and covered.issubset(allowed_msg_ids):
                        out.append(entry)
                        return
                    if covered and covered.intersection(allowed_msg_ids):
                        sidecar = meta.get("sidecar") or ""
                        for raw in load_sidecar_entries(sidecar):
                            append_entry(raw)
                        return
                    if not covered:
                        ts = entry.get("timestamp", "")
                        if ts and anchor_ts and ts <= anchor_ts:
                            out.append(entry)
                    return

                mid = entry_msg_id(entry)
                if mid:
                    if mid in allowed_msg_ids:
                        out.append(entry)
                    return

                # 兼容升级前的 ctx: 无 msg_id 时只能按时间保留切点前缀。
                ts = entry.get("timestamp", "")
                if ts and anchor_ts and ts <= anchor_ts:
                    out.append(entry)

            for c in src_ctx:
                append_entry(c)
            return out

        linked_ctx = build_linked_context() if has_linked_ctx else []
        if has_linked_ctx and linked_ctx:
            ctx_slice = linked_ctx
        else:
            ctx_slice = None

        # anchor 的文本内容（用于内容匹配）
        anchor_text = ""
        for seg in anchor_msg.get("segments", []) or []:
            if seg.get("type") == "text":
                anchor_text += seg.get("content", "") or ""
        anchor_prefix = anchor_text.strip()[:60]

        ctx_idx = -1

        if ctx_slice is None and anchor_idx == len(src_msgs) - 1:
            ctx_idx = len(src_ctx)

        # 策略 1: 内容匹配（旧数据兜底）
        if ctx_slice is None and ctx_idx < 0 and anchor_prefix:
            for i in range(len(src_ctx) - 1, -1, -1):
                c = src_ctx[i]
                if c.get("role") != "assistant":
                    continue
                ct = (c.get("content") or "").strip()
                if not ct:
                    continue
                # 双向前缀匹配（任一方为另一方的开头即可）
                head_a = anchor_prefix[:30]
                head_c = ct[:30]
                if ct.startswith(head_a) or anchor_prefix.startswith(head_c):
                    ctx_idx = i + 1
                    # 同消息的后续 tool 结果一起带上
                    while (ctx_idx < len(src_ctx)
                           and src_ctx[ctx_idx].get("role") == "tool"):
                        ctx_idx += 1
                    break

        # 策略 2: 时间戳兜底（取 <= anchor_ts 的最后一条）
        if ctx_slice is None and ctx_idx < 0 and anchor_ts:
            for i in range(len(src_ctx) - 1, -1, -1):
                ts = src_ctx[i].get("timestamp", "")
                if ts and ts <= anchor_ts:
                    ctx_idx = i + 1
                    while (ctx_idx < len(src_ctx)
                           and src_ctx[ctx_idx].get("role") == "tool"):
                        ctx_idx += 1
                    break

        if ctx_slice is None and ctx_idx < 0:
            ctx_idx = 0  # 宁可空也别错带后续

        # 继承被保留消息的累计 tokens（让标题栏与气泡显示一致）
        inherited_tokens = 0
        inherited_last_prompt = 0
        inherited_last_completion = 0
        inherited_last_msg = 0
        for m in msgs_slice:
            meta = m.get("metadata") or {}
            try:
                inherited_tokens += int(meta.get("tokens") or 0)
            except (TypeError, ValueError):
                pass
            # 最后一条有 prompt_tokens 的 assistant 消息代表“当前 ctx”
            if m.get("role") == "assistant":
                try:
                    p = int(meta.get("prompt_tokens") or 0)
                    c = int(meta.get("completion_tokens") or 0)
                    t = int(meta.get("tokens") or 0)
                except (TypeError, ValueError):
                    p = c = t = 0
                if p > 0:
                    inherited_last_prompt = p
                    inherited_last_completion = c
                if t > 0:
                    inherited_last_msg = t

        new_sid = uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc).isoformat()
        session = {
            "id": new_sid,
            "title": title,
            "tokens": inherited_tokens,
            "last_prompt_tokens": inherited_last_prompt,
            "last_completion_tokens": inherited_last_completion,
            "last_msg_tokens": inherited_last_msg,
            "forked_from": src_id,
            "created_at": now,
            "updated_at": now,
        }

        with self._lock:
            data = self._load_sessions_raw()
            data["sessions"].append(session)
            self._save_sessions_raw(data)

        msg_data = {
            "messages": msgs_slice,
            "context": ctx_slice if ctx_slice is not None else src_ctx[:ctx_idx],
        }
        self._save_message_file(new_sid, msg_data)
        return session

    # ==================== 前端消息管理 (messages[]) ====================

    def get_messages(self, session_id: str) -> List[Dict]:
        """获取前端展示的消息历史"""
        if not _VALID_SID.match(session_id):
            return []
        data = self._load_message_file(session_id)
        return data.get("messages", [])

    def add_message(self, session_id: str, role: str, content: str = "",
                    msg_id: Optional[str] = None,
                    tool_calls: Optional[List[Dict]] = None,
                    segments: Optional[List[Dict]] = None,
                    metadata: Optional[Dict] = None):
        """
        添加前端展示消息

        Args:
            session_id: 会话 ID
            role: 角色 (user / assistant)
            content: 消息内容 (legacy, 当 segments 为空时使用)
            msg_id: 消息 ID
            tool_calls: 工具调用记录 (legacy, 当 segments 为空时使用)
            segments: 分段列表 [{type:'text',content:''}, {type:'tool',...}]
            metadata: 附加元数据
        """
        with self._lock:
            data = self._load_message_file(session_id)

            entry: Dict[str, Any] = {
                "role": role,
                "timestamp": datetime.now().isoformat(),
            }
            if msg_id:
                entry["id"] = msg_id

            if segments is not None:
                # 新格式: segments
                for seg in segments:
                    if seg.get("type") == "text":
                        seg["content"] = self._truncate_content(
                            seg.get("content", ""))
                entry["segments"] = segments
            else:
                # 旧格式: content + tool_calls (向后兼容)
                entry["content"] = self._truncate_content(content)
                if tool_calls:
                    entry["tool_calls"] = tool_calls

            if metadata:
                entry["metadata"] = metadata

            data["messages"].append(entry)

            # 消息数量限制
            if len(data["messages"]) > MAX_MESSAGES_PER_SESSION:
                first_msg = data["messages"][0]
                keep_count = MAX_MESSAGES_PER_SESSION - 1
                data["messages"] = [first_msg] + data["messages"][-keep_count:]

            self._save_message_file(session_id, data)

    def append_to_message(self, session_id: str, msg_id: str,
                          segments: List[Dict],
                          add_tokens: int = 0,
                          metadata_update: Optional[Dict] = None):
        """将额外 segments 追加到已存在的消息（用于危险工具确认后续）。

        若 msg_id 不存在则静默忽略。
        """
        if not _VALID_SID.match(session_id):
            return
        with self._lock:
            data = self._load_message_file(session_id)
            for entry in data["messages"]:
                if entry.get("id") != msg_id:
                    continue
                existing = entry.get("segments") or []
                for seg in segments:
                    if seg.get("type") == "text":
                        seg["content"] = self._truncate_content(
                            seg.get("content", ""))
                entry["segments"] = existing + segments
                if add_tokens:
                    meta = entry.setdefault("metadata", {})
                    meta["tokens"] = (meta.get("tokens", 0) or 0) + add_tokens
                if metadata_update:
                    meta = entry.setdefault("metadata", {})
                    for key, value in metadata_update.items():
                        if value is not None:
                            meta[key] = value
                self._save_message_file(session_id, data)
                return

    # ==================== AI 上下文管理 (context[]) ====================

    def get_context(self, session_id: str,
                    max_entries: Optional[int] = None) -> List[Dict]:
        """
        获取 AI 引擎上下文消息

        包含所有中间推理: 工具选择、参数描述注入、工具执行结果等。
        用于在下一轮 run() 中恢复完整 AI 对话上下文。

        Returns:
            [{"role": "user/assistant", "content": "...", "metadata": {...}}]
        """
        data = self._load_message_file(session_id)
        context = self._sanitize_context_protocol(data.get("context", []))
        if max_entries and len(context) > max_entries:
            context = self._trim_context_preserving_protocol(context, max_entries)
        return context

    def add_context(self, session_id: str, role: str, content: str,
                    metadata: Optional[Dict] = None):
        """
        添加 AI 上下文消息

        与 add_message() 分离: 上下文消息仅用于 AI 推理, 不展示在前端。
        包括: 用户原始请求、AI 中间回复、工具描述注入、工具执行结果。

        Args:
            session_id: 会话 ID
            role: 角色 (user / assistant)
            content: 消息内容
            metadata: 附加元数据 (如 phase, type 等)
        """
        content = self._truncate_content(content)

        with self._lock:
            data = self._load_message_file(session_id)

            entry: Dict[str, Any] = {
                "role": role,
                "content": content,
                "timestamp": datetime.now().isoformat(),
            }
            if metadata:
                entry["metadata"] = metadata

            data["context"].append(entry)

            data["context"] = self._trim_context_preserving_protocol(data["context"])

            self._save_message_file(session_id, data)

    def add_context_entry(self, session_id: str, entry: Dict):
        """
        添加任意结构的上下文条目（用于原生 tool_calls 消息）

        与 add_context() 不同，此方法直接存储整个 dict，
        支持 tool_calls / tool_call_id / name 等字段。

        Args:
            session_id: 会话 ID
            entry: 任意 dict，必须包含 "role" 字段
        """
        with self._lock:
            data = self._load_message_file(session_id)

            ts_entry = dict(entry)
            ts_entry.setdefault("timestamp", datetime.now().isoformat())

            # content 字段截断（若存在且为 str）
            if isinstance(ts_entry.get("content"), str):
                ts_entry["content"] = self._truncate_content(ts_entry["content"])
            if isinstance(ts_entry.get("reasoning_content"), str):
                ts_entry["reasoning_content"] = self._truncate_content(
                    ts_entry["reasoning_content"])

            data["context"].append(ts_entry)

            data["context"] = self._trim_context_preserving_protocol(data["context"])

            self._save_message_file(session_id, data)

    def set_context(self, session_id: str, entries: List[Dict]):
        """整体替换会话上下文 (用于压缩后写回精简历史)。"""
        with self._lock:
            data = self._load_message_file(session_id)
            normalized: List[Dict] = []
            for e in entries:
                ne = dict(e)
                ne.setdefault("timestamp", datetime.now().isoformat())
                if isinstance(ne.get("content"), str):
                    ne["content"] = self._truncate_content(ne["content"])
                if isinstance(ne.get("reasoning_content"), str):
                    ne["reasoning_content"] = self._truncate_content(
                        ne["reasoning_content"])
                normalized.append(ne)
            normalized = self._trim_context_preserving_protocol(normalized)
            data["context"] = normalized
            self._save_message_file(session_id, data)

    # ==================== 内部方法 ====================

    @staticmethod
    def _tool_call_ids(entry: Dict) -> List[str]:
        ids: List[str] = []
        for tc in entry.get("tool_calls") or []:
            tc_id = tc.get("id")
            if tc_id:
                ids.append(str(tc_id))
        return ids

    def _sanitize_context_protocol(self, context: List[Dict]) -> List[Dict]:
        """移除会导致 Chat Completions 400 的孤立/不完整 tool 片段。"""
        sanitized: List[Dict] = []
        i = 0
        while i < len(context):
            entry = context[i]
            role = entry.get("role")

            if role == "tool":
                i += 1
                continue

            if role == "assistant" and entry.get("tool_calls"):
                expected = self._tool_call_ids(entry)
                if not expected:
                    i += 1
                    continue
                block = [entry]
                seen = set()
                j = i + 1
                while j < len(context) and context[j].get("role") == "tool":
                    tool_call_id = str(context[j].get("tool_call_id") or "")
                    if tool_call_id not in expected or tool_call_id in seen:
                        break
                    block.append(context[j])
                    seen.add(tool_call_id)
                    j += 1
                    if len(seen) == len(expected):
                        break
                if len(seen) == len(expected):
                    sanitized.extend(block)
                    i = j
                else:
                    i += 1
                continue

            sanitized.append(entry)
            i += 1
        return sanitized

    def _trim_context_preserving_protocol(self, context: List[Dict],
                                          max_entries: int = MAX_CONTEXT_PER_SESSION) -> List[Dict]:
        """按协议块裁剪 context，不从 assistant(tool_calls)+tool 组中间切开。"""
        if len(context) <= max_entries:
            return context

        blocks: List[List[Dict]] = []
        i = 0
        while i < len(context):
            entry = context[i]
            if entry.get("role") == "assistant" and entry.get("tool_calls"):
                expected = set(self._tool_call_ids(entry))
                block = [entry]
                i += 1
                while i < len(context) and context[i].get("role") == "tool":
                    tool_call_id = str(context[i].get("tool_call_id") or "")
                    if tool_call_id not in expected:
                        break
                    block.append(context[i])
                    i += 1
                blocks.append(block)
                continue
            blocks.append([entry])
            i += 1

        kept: List[Dict] = []
        for block in reversed(blocks):
            if kept and len(kept) + len(block) > max_entries:
                break
            kept = block + kept
        return kept

    def _truncate_content(self, content: str) -> str:
        """截断过大的单条消息"""
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_MESSAGE_SIZE_BYTES:
                half = MAX_MESSAGE_SIZE_BYTES // 2
                truncated = content_bytes[:half].decode("utf-8", errors="ignore")
                content = (truncated
                           + f"\n\n[截断，原始大小: {len(content_bytes)} 字节]")
        return content

    def _init_messages(self, session_id: str):
        """初始化消息文件"""
        msg_file = self.messages_dir / f"{session_id}.json"
        if not msg_file.exists():
            self._save_json(msg_file,
                            {"messages": [], "context": []})

    def _load_message_file(self, session_id: str) -> Dict:
        """加载消息文件 (带默认值)"""
        msg_file = self.messages_dir / f"{session_id}.json"
        if not msg_file.exists():
            return {"messages": [], "context": []}
        try:
            with msg_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
                # 兼容旧格式: 确保 context 键存在
                if "context" not in data:
                    data["context"] = []
                if "messages" not in data:
                    data["messages"] = []
                return data
        except (json.JSONDecodeError, IOError):
            return {"messages": [], "context": []}

    def _save_message_file(self, session_id: str, data: Dict):
        """保存消息文件"""
        msg_file = self.messages_dir / f"{session_id}.json"
        self._save_json(msg_file, data)

        # 文件大小检查
        try:
            file_size_mb = msg_file.stat().st_size / (1024 * 1024)
            if file_size_mb > MAX_HISTORY_FILE_SIZE_MB:
                self._compact_messages(msg_file, data)
        except OSError:
            pass

    def _load_sessions_raw(self) -> Dict:
        """加载会话列表"""
        try:
            with self.sessions_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"sessions": []}

    def _save_sessions_raw(self, data: Dict):
        """原子保存会话列表"""
        self._save_json(self.sessions_file, data)

    def _save_json(self, filepath: Path, data: Dict):
        """原子化保存 JSON（跨平台，带重试）"""
        temp_path = filepath.with_suffix(".tmp")

        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                os.replace(temp_path, filepath)
                return
            except (PermissionError, OSError):
                if attempt < max_retries - 1:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise

    def _compact_messages(self, filepath: Path, data: Dict):
        """压缩消息历史"""
        # 前端消息: 保留首条 + 最近 100 条
        messages = data.get("messages", [])
        if len(messages) > 101:
            first = messages[0]
            data["messages"] = [first] + messages[-100:]

        # AI 上下文: 保留最近 200 条，同时保持 tool_calls/tool 协议完整
        context = data.get("context", [])
        data["context"] = self._trim_context_preserving_protocol(
            self._sanitize_context_protocol(context)
        )

        self._save_json(filepath, data)
