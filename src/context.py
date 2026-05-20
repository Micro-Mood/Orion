"""
Orion 上下文管理
================

管理 AI 对话上下文：FIFO 滑动窗口 + 已注册工具表。
system_msg 不计入 FIFO，始终在最前。
支持原生 OpenAI tool_calls 消息格式。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import json


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

    - system_msg: 系统提示（始终在最前）
    - history: 完整历史; 不做 FIFO 裁剪, 超长由 token 压缩机制处理
    - registered_tools: {name: idle_rounds} 已注册工具及空闲轮数
    - confirmed_tools: 本轮已确认的危险工具名集合
    """
    system_msg: Optional[Message] = None
    history: List[Message] = field(default_factory=list)
    registered_tools: Dict[str, int] = field(default_factory=dict)
    confirmed_tools: set = field(default_factory=set)

    def set_system(self, content: str):
        """设置系统提示"""
        self.system_msg = Message(role="system", content=content)

    def add_user(self, content: str):
        """添加用户消息"""
        self.history.append(Message(role="user", content=content))

    def add_assistant(self, content: str):
        """添加纯文本 AI 回复"""
        self.history.append(Message(role="assistant", content=content))

    def add_tool_call_assistant(self, tool_calls: List[Dict],
                                content: Optional[str] = None):
        """添加带 tool_calls 的 assistant 消息"""
        self.history.append(Message(role="assistant", content=content,
                                    tool_calls=tool_calls))

    def add_tool_result(self, tool_call_id: str, name: str, content: str):
        """添加 role=tool 消息（工具执行结果）"""
        self.history.append(Message(role="tool", content=content,
                                    tool_call_id=tool_call_id, name=name))

    def add_system_note(self, content: str):
        """添加系统注入消息（如连续失败提示）"""
        self.history.append(Message(role="system", content=content))

    def get_last_tool_calls(self) -> Optional[List[Dict]]:
        """获取最后一条带 tool_calls 的 assistant 消息"""
        for msg in reversed(self.history):
            if msg.role == "assistant" and msg.tool_calls:
                return msg.tool_calls
        return None

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
        """重置临时状态（仅清除已确认的危险工具）。
        注意：registered_tools 跨轮保留，不在此重置。
        """
        self.confirmed_tools.clear()

    def clear_history(self):
        """清空历史（保留 system_msg 与 registered_tools）"""
        self.history = []
        self.reset_phase()

    # ==================== 已注册工具管理 ====================

    def register(self, names: List[str]) -> List[str]:
        """注册工具。返回本次新增的名字（已注册的不计，但会重置 idle）。"""
        new: List[str] = []
        for n in names:
            if not isinstance(n, str) or not n:
                continue
            if n not in self.registered_tools:
                new.append(n)
            self.registered_tools[n] = 0
        return new

    def unregister(self, names: List[str]) -> List[str]:
        """卸载工具。返回实际卸载的名字。"""
        removed: List[str] = []
        for n in names:
            if n in self.registered_tools:
                self.registered_tools.pop(n, None)
                removed.append(n)
        return removed

    def age_and_evict(self, ttl: int) -> List[str]:
        """所有已注册工具 idle+1，超过 ttl 则卸载。ttl<=0 不卸载。"""
        if ttl <= 0:
            return []
        evicted: List[str] = []
        for n in list(self.registered_tools.keys()):
            self.registered_tools[n] += 1
            if self.registered_tools[n] > ttl:
                self.registered_tools.pop(n, None)
                evicted.append(n)
        return evicted

    def touch_tool(self, name: str):
        """工具被调用时重置 idle 计数。"""
        if name in self.registered_tools:
            self.registered_tools[name] = 0

    def token_estimate(self) -> int:
        """粗略估计 token 数 (中英文混合: len // 3)。

        包含 system_msg + history 中的 content 与 tool_calls JSON 序列化长度。
        tool_calls 可能是几百到几千 tokens, 不算会严重低估。
        """
        total = 0
        if self.system_msg and self.system_msg.content:
            total += len(self.system_msg.content)
        for msg in self.history:
            if msg.content:
                total += len(msg.content)
            if msg.tool_calls:
                try:
                    total += len(json.dumps(msg.tool_calls, ensure_ascii=False))
                except (TypeError, ValueError):
                    pass
        return total // 3

    def needs_compression(self, context_window: int, compress_at: float,
                           real_prompt_tokens: int = 0) -> bool:
        """当前 token 数占模型窗口比例 >= compress_at 时返回 True。

        优先使用 LLM API 返回的真实 prompt_tokens (最准确),
        没有时 fallback 到字符数估算 (会严重低估中文)。
        """
        if context_window <= 0 or compress_at <= 0:
            return False
        threshold = int(context_window * compress_at)
        actual = real_prompt_tokens if real_prompt_tokens > 0 else self.token_estimate()
        return actual >= threshold
