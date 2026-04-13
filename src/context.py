"""
Orion 上下文管理
================

管理 AI 对话上下文：Phase 状态机 + FIFO 滑动窗口。
system_msg 不计入 FIFO，始终在最前。
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
    """对话消息"""
    role: str       # "system" | "user" | "assistant"
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class Context:
    """
    对话上下文管理器
    
    - system_msg: 系统提示（不计入 FIFO，始终在最前）
    - history: FIFO 滑动窗口，保留最近 max_history 条消息
    - phase: 当前引擎阶段
    - selected_tools: 当前选中的工具列表
    """
    max_history: int = 20
    system_msg: Optional[Message] = None
    history: List[Message] = field(default_factory=list)
    phase: Phase = Phase.SELECT
    selected_tools: List[str] = field(default_factory=list)

    def set_system(self, content: str):
        """设置系统提示"""
        self.system_msg = Message(role="system", content=content)

    def add_user(self, content: str):
        """添加用户消息（含系统注入的工具描述、执行结果等）"""
        self.history.append(Message(role="user", content=content))
        self._trim()

    def add_assistant(self, content: str):
        """添加 AI 回复"""
        self.history.append(Message(role="assistant", content=content))
        self._trim()

    def add_system_note(self, content: str):
        """添加系统注入消息（如工具说明、格式修正、工具结果）。"""
        self.history.append(Message(role="system", content=content))
        self._trim()

    def _trim(self):
        """FIFO 裁剪：保留最近 max_history 条"""
        if len(self.history) > self.max_history:
            excess = len(self.history) - self.max_history
            self.history = self.history[excess:]

    def build_messages(self) -> List[Dict[str, str]]:
        """构建给 LLM API 的消息列表"""
        messages = []
        if self.system_msg:
            messages.append(self.system_msg.to_dict())
        for msg in self.history:
            messages.append(msg.to_dict())
        return messages

    def get_last_assistant_msg(self) -> Optional[str]:
        """获取最后一条 AI 消息"""
        for msg in reversed(self.history):
            if msg.role == "assistant":
                return msg.content
        return None

    def reset_phase(self):
        """重置到 SELECT 阶段"""
        self.phase = Phase.SELECT
        self.selected_tools = []

    def clear_history(self):
        """清空历史（保留 system_msg）"""
        self.history = []
        self.reset_phase()

    def token_estimate(self) -> int:
        """粗略估计 token 数（中英文混合: len // 3）"""
        total = 0
        if self.system_msg:
            total += len(self.system_msg.content)
        for msg in self.history:
            total += len(msg.content)
        return total // 3
