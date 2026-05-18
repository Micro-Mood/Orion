"""
Orion 上下文管理
================

管理 AI 对话上下文：Phase 状态机 + FIFO 滑动窗口。
system_msg 不计入 FIFO，始终在最前。
支持原生 OpenAI tool_calls 消息格式。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Phase(Enum):
    """引擎阶段"""
    SELECT = "select"    # 工具选择
    PARAMS = "params"    # 参数填写
    EXEC = "exec"        # 执行工具


@dataclass
class Message:
    """对话消息（支持原生 tool_calls 格式）"""
    role: str       # "system" | "user" | "assistant" | "tool"
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None   # assistant with tool_calls
    tool_call_id: Optional[str] = None        # role=tool 时
    name: Optional[str] = None               # role=tool 时工具名

    def to_dict(self) -> Dict:
        d: Dict = {"role": self.role}
        if self.tool_calls:
            # assistant 带 tool_calls：content 可为 None
            if self.content is not None:
                d["content"] = self.content
            d["tool_calls"] = self.tool_calls
        else:
            d["content"] = self.content if self.content is not None else ""
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class Context:
    """
    对话上下文管理器

    - system_msg: 系统提示（不计入 FIFO，始终在最前）
    - history: FIFO 滑动窗口，保留最近 max_history 条消息
    - phase: 当前引擎阶段
    - selected_tools: 当前选中的工具列表
    - confirmed_tools: 本轮已确认的危险工具名集合
    """
    max_history: int = 20
    system_msg: Optional[Message] = None
    history: List[Message] = field(default_factory=list)
    phase: Phase = Phase.SELECT
    selected_tools: List[str] = field(default_factory=list)
    confirmed_tools: set = field(default_factory=set)

    def set_system(self, content: str):
        """设置系统提示"""
        self.system_msg = Message(role="system", content=content)

    def add_user(self, content: str):
        """添加用户消息"""
        self.history.append(Message(role="user", content=content))
        self._trim()

    def add_assistant(self, content: str):
        """添加纯文本 AI 回复"""
        self.history.append(Message(role="assistant", content=content))
        self._trim()

    def add_tool_call_assistant(self, tool_calls: List[Dict],
                                content: Optional[str] = None):
        """添加带 tool_calls 的 assistant 消息（PARAMS 阶段输出）"""
        self.history.append(Message(role="assistant", content=content,
                                    tool_calls=tool_calls))
        self._trim()

    def add_tool_result(self, tool_call_id: str, name: str, content: str):
        """添加 role=tool 消息（EXEC 阶段工具执行结果）"""
        self.history.append(Message(role="tool", content=content,
                                    tool_call_id=tool_call_id, name=name))
        self._trim()

    def add_system_note(self, content: str):
        """添加系统注入消息（如连续失败提示）"""
        self.history.append(Message(role="system", content=content))
        self._trim()

    def get_last_tool_calls(self) -> Optional[List[Dict]]:
        """获取最后一条带 tool_calls 的 assistant 消息"""
        for msg in reversed(self.history):
            if msg.role == "assistant" and msg.tool_calls:
                return msg.tool_calls
        return None

    def _trim(self):
        """FIFO 裁剪：保留最近 max_history 条"""
        if len(self.history) > self.max_history:
            excess = len(self.history) - self.max_history
            self.history = self.history[excess:]

    def build_messages(self) -> List[Dict]:
        """构建给 LLM API 的消息列表"""
        messages = []
        if self.system_msg:
            messages.append(self.system_msg.to_dict())
        for msg in self.history:
            messages.append(msg.to_dict())
        return messages

    def get_last_assistant_msg(self) -> Optional[str]:
        """获取最后一条纯文本 AI 消息"""
        for msg in reversed(self.history):
            if msg.role == "assistant" and not msg.tool_calls:
                return msg.content
        return None

    def reset_phase(self):
        """重置到 SELECT 阶段"""
        self.phase = Phase.SELECT
        self.selected_tools = []
        self.confirmed_tools.clear()

    def clear_history(self):
        """清空历史（保留 system_msg）"""
        self.history = []
        self.reset_phase()

    def token_estimate(self) -> int:
        """粗略估计 token 数（中英文混合: len // 3）"""
        total = 0
        if self.system_msg and self.system_msg.content:
            total += len(self.system_msg.content)
        for msg in self.history:
            if msg.content:
                total += len(msg.content)
        return total // 3
