"""
Orion 会话持久化
================

JSON 文件存储，会话与消息分离。
- data/sessions.json: 会话元数据列表
- data/messages/{session_id}.json: 每个会话的消息
  - messages[]: 前端展示的消息 (用户可见)
  - context[]: AI 引擎上下文消息 (包含中间推理、工具注入、执行结果)

支持原子写入（Windows 兼容）和自动截断。
"""

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 数据目录: Orion/data/
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 限制常量
MAX_MESSAGES_PER_SESSION = 500
MAX_CONTEXT_PER_SESSION = 200
MAX_MESSAGE_SIZE_BYTES = 50 * 1024  # 50KB
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
        now = datetime.now().isoformat()
        session = {
            "id": session_id,
            "title": title,
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
                    s["updated_at"] = datetime.now().isoformat()
                    self._save_sessions_raw(data)
                    return True
            return False

    def delete_session(self, session_id: str) -> bool:
        """删除会话及其消息"""
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

    # ==================== 前端消息管理 (messages[]) ====================

    def get_messages(self, session_id: str) -> List[Dict]:
        """获取前端展示的消息历史"""
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
        context = data.get("context", [])
        if max_entries and len(context) > max_entries:
            context = context[-max_entries:]
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

            # 上下文数量限制
            if len(data["context"]) > MAX_CONTEXT_PER_SESSION:
                data["context"] = data["context"][-MAX_CONTEXT_PER_SESSION:]

            self._save_message_file(session_id, data)

    def get_context_messages(self, session_id: str,
                             max_messages: int = 20) -> List[Dict]:
        """
        兼容接口: 获取用于 AI 上下文的消息 (最近 N 条 user/assistant)

        优先使用 get_context()。此方法作为降级备用。
        """
        all_msgs = self.get_messages(session_id)
        context_msgs = []
        for msg in all_msgs:
            role = msg.get("role", "")
            if role in ("user", "assistant"):
                context_msgs.append({
                    "role": role,
                    "content": msg.get("content", ""),
                })

        if len(context_msgs) > max_messages:
            context_msgs = context_msgs[-max_messages:]

        return context_msgs

    # ==================== 内部方法 ====================

    def _truncate_content(self, content: str) -> str:
        """截断过大的单条消息"""
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
            if len(content_bytes) > MAX_MESSAGE_SIZE_BYTES:
                half = MAX_MESSAGE_SIZE_BYTES // 2
                content = (content[:half]
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
        """原子化保存 JSON（Windows 兼容，带重试）"""
        temp_path = filepath.with_suffix(".tmp")

        with temp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                if os.name == "nt":
                    if filepath.exists():
                        backup = filepath.with_suffix(".bak")
                        shutil.copy2(filepath, backup)
                    shutil.move(str(temp_path), str(filepath))
                else:
                    os.replace(temp_path, filepath)
                return
            except (PermissionError, OSError):
                if attempt < max_retries - 1:
                    import time
                    time.sleep(0.1 * (2 ** attempt))
                else:
                    raise

    def _compact_messages(self, filepath: Path, data: Dict):
        """压缩消息历史"""
        # 前端消息: 保留首条 + 最近 100 条
        messages = data.get("messages", [])
        if len(messages) > 101:
            first = messages[0]
            data["messages"] = [first] + messages[-100:]

        # AI 上下文: 保留最近 200 条
        context = data.get("context", [])
        if len(context) > MAX_CONTEXT_PER_SESSION:
            data["context"] = context[-MAX_CONTEXT_PER_SESSION:]

        self._save_json(filepath, data)
