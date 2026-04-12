"""
Orion 工具注册表
================

注册 Axon MCP Server 的 28 个工具方法 + 控制指令。
使用紧凑描述格式，SELECT 阶段只传工具名，PARAMS 阶段传 compact desc，节省 token。
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ToolParam:
    """工具参数"""
    name: str
    type: str
    desc: str
    required: bool = True
    default: Optional[str] = None


@dataclass
class Tool:
    """工具定义"""
    name: str
    desc: str
    params: List[ToolParam]
    category: str

    def to_compact(self) -> str:
        """
        生成紧凑描述格式，节省 token。
        
        格式: name|desc|param:type*说明;param:type=default,说明
        * 表示必填，= 后跟默认值表示选填
        """
        parts = []
        for p in self.params:
            if p.required:
                parts.append(f"{p.name}:{p.type}*{p.desc}")
            else:
                default_str = f"={p.default}" if p.default else ""
                parts.append(f"{p.name}:{p.type}{default_str},{p.desc}")
        params_str = ";".join(parts) if parts else "(无参数)"
        return f"{self.name}|{self.desc}|{params_str}"


# ==================== 全局注册表 ====================

TOOLS: Dict[str, Tool] = {}


def register(name: str, desc: str, params: List[ToolParam], category: str):
    """注册工具"""
    TOOLS[name] = Tool(name=name, desc=desc, params=params, category=category)


def get_tool(name: str) -> Optional[Tool]:
    """获取工具定义"""
    return TOOLS.get(name)


def get_all_names() -> List[str]:
    """获取所有工具名"""
    return list(TOOLS.keys())


def get_names_by_category() -> Dict[str, List[str]]:
    """按分类获取工具名"""
    categories: Dict[str, List[str]] = {}
    for tool in TOOLS.values():
        categories.setdefault(tool.category, []).append(tool.name)
    return categories


def get_names_of_category(category: str) -> List[str]:
    """获取指定分类的工具名列表"""
    return [t.name for t in TOOLS.values() if t.category == category]


def get_compact_desc(names: List[str]) -> str:
    """获取指定工具的紧凑描述"""
    lines = []
    for name in names:
        tool = TOOLS.get(name)
        if tool:
            lines.append(tool.to_compact())
    return "\n".join(lines)


# ==================== 工具注册 ====================

def _init_tools():
    """注册所有工具（匹配 Axon MCP Server 的 27 个方法 + 控制指令）"""

    # ==================== 文件操作 (file) — 13 个 ====================

    register("read_file", "读取文件内容", [
        ToolParam("path", "str", "文件路径"),
        ToolParam("encoding", "str", "编码", False, "utf-8"),
        ToolParam("line_range", "list", "行范围[start,end]", False),
        ToolParam("max_size", "int", "最大字节数", False),
    ], "file")

    register("write_file", "写入文件(不存在则创建)", [
        ToolParam("path", "str", "文件路径"),
        ToolParam("content", "str", "文件内容"),
        ToolParam("encoding", "str", "编码", False, "utf-8"),
    ], "file")

    register("stat_path", "获取文件/目录状态信息", [
        ToolParam("path", "str", "路径"),
        ToolParam("follow_symlinks", "bool", "是否跟随符号链接", False, "true"),
    ], "file")

    register("list_directory", "列出目录内容", [
        ToolParam("path", "str", "目录路径"),
        ToolParam("pattern", "str", "匹配模式", False),
        ToolParam("recursive", "bool", "是否递归", False, "false"),
        ToolParam("include_hidden", "bool", "包含隐藏文件", False, "false"),
        ToolParam("max_results", "int", "最大返回数", False),
    ], "file")

    register("delete_file", "删除文件", [
        ToolParam("path", "str", "文件路径"),
    ], "file")

    register("delete_directory", "删除目录", [
        ToolParam("path", "str", "目录路径"),
        ToolParam("recursive", "bool", "是否递归删除", False, "false"),
        ToolParam("force", "bool", "强制删除", False, "false"),
    ], "file")

    register("move_file", "移动/重命名文件", [
        ToolParam("source", "str", "源路径"),
        ToolParam("dest", "str", "目标路径"),
        ToolParam("overwrite", "bool", "是否覆盖", False, "false"),
    ], "file")

    register("copy_file", "复制文件", [
        ToolParam("source", "str", "源路径"),
        ToolParam("dest", "str", "目标路径"),
        ToolParam("overwrite", "bool", "是否覆盖", False, "false"),
    ], "file")

    register("create_directory", "创建目录", [
        ToolParam("path", "str", "目录路径"),
        ToolParam("recursive", "bool", "递归创建", False, "true"),
    ], "file")

    register("replace_string_in_file", "文本匹配替换(old_string必须唯一)", [
        ToolParam("path", "str", "文件路径"),
        ToolParam("old_string", "str", "要替换的原始文本"),
        ToolParam("new_string", "str", "替换为的新文本"),
        ToolParam("encoding", "str", "编码", False, "utf-8"),
    ], "file")

    register("multi_replace_string_in_file", "批量文本替换", [
        ToolParam("replacements", "list", "替换列表[{path,old_string,new_string}]"),
        ToolParam("encoding", "str", "编码", False, "utf-8"),
    ], "file")

    register("move_directory", "移动/重命名目录", [
        ToolParam("source", "str", "源路径"),
        ToolParam("dest", "str", "目标路径"),
    ], "file")

    # ==================== 搜索操作 (search) — 3 个 ====================

    register("find_files", "按模式搜索文件", [
        ToolParam("pattern", "str", "匹配模式(glob)"),
        ToolParam("root", "str", "搜索根目录", False),
        ToolParam("recursive", "bool", "是否递归", False, "true"),
        ToolParam("file_types", "list", "文件类型过滤", False),
        ToolParam("include_hidden", "bool", "包含隐藏文件", False, "false"),
        ToolParam("max_results", "int", "最大返回数", False),
    ], "search")

    register("search_text", "在文件中搜索文本内容", [
        ToolParam("query", "str", "搜索内容"),
        ToolParam("root", "str", "搜索根目录", False),
        ToolParam("file_pattern", "str", "文件匹配模式", False, "*"),
        ToolParam("case_sensitive", "bool", "区分大小写", False, "false"),
        ToolParam("is_regex", "bool", "是否正则", False, "false"),
        ToolParam("context_lines", "int", "上下文行数", False, "2"),
        ToolParam("include_hidden", "bool", "包含隐藏文件", False, "false"),
        ToolParam("max_results", "int", "最大返回数", False),
    ], "search")

    register("find_symbol", "搜索代码符号(函数/类/变量)", [
        ToolParam("symbol", "str", "符号名"),
        ToolParam("root", "str", "搜索根目录", False),
        ToolParam("symbol_type", "str", "符号类型", False),
        ToolParam("file_pattern", "str", "文件匹配模式", False, "*"),
        ToolParam("include_hidden", "bool", "包含隐藏文件", False, "false"),
        ToolParam("max_results", "int", "最大返回数", False),
    ], "search")

    # ==================== 命令执行 (command) — 10 个 ====================

    register("run_command", "执行命令并等待完成", [
        ToolParam("command", "str", "命令"),
        ToolParam("cwd", "str", "工作目录", False),
        ToolParam("timeout", "int", "超时毫秒", False),
        ToolParam("env", "dict", "环境变量", False),
    ], "command")

    register("create_task", "创建后台进程", [
        ToolParam("command", "str", "命令"),
        ToolParam("cwd", "str", "工作目录", False),
        ToolParam("timeout", "int", "超时毫秒", False),
        ToolParam("env", "dict", "环境变量", False),
    ], "command")

    register("stop_task", "停止进程", [
        ToolParam("task_id", "str", "任务ID"),
        ToolParam("force", "bool", "是否强制", False, "false"),
    ], "command")

    register("wait_task", "等待进程完成", [
        ToolParam("task_id", "str", "任务ID"),
        ToolParam("timeout", "int", "超时毫秒", False),
    ], "command")

    register("task_status", "查询进程状态", [
        ToolParam("task_id", "str", "任务ID"),
    ], "command")

    register("read_stdout", "读取进程标准输出", [
        ToolParam("task_id", "str", "任务ID"),
        ToolParam("max_chars", "int", "最大字符数", False, "8192"),
    ], "command")

    register("read_stderr", "读取进程标准错误", [
        ToolParam("task_id", "str", "任务ID"),
        ToolParam("max_chars", "int", "最大字符数", False, "8192"),
    ], "command")

    register("write_stdin", "向进程写入输入", [
        ToolParam("task_id", "str", "任务ID"),
        ToolParam("data", "str", "输入数据"),
        ToolParam("eof", "bool", "是否发送EOF", False, "false"),
    ], "command")

    register("list_tasks", "列出所有进程", [], "command")

    register("del_task", "删除已结束进程", [
        ToolParam("task_id", "str", "任务ID"),
    ], "command")

    # ==================== 系统信息 (system) — 1 个 ====================

    register("get_system_info", "获取系统信息", [], "system")

    # ==================== 网络 (web) — 1 个 ====================

    register("fetch_webpage", "抓取网页正文内容", [
        ToolParam("url", "str", "网页URL"),
        ToolParam("query", "str", "搜索关键词", False),
    ], "web")

    # ==================== 控制指令 (ctrl) — 2 个 ====================

    register("done", "完成回复，结束当前轮对话", [
        ToolParam("summary", "str", "回复内容摘要"),
    ], "ctrl")

    register("ask", "向用户提问，等待回答后继续", [
        ToolParam("question", "str", "问题"),
        ToolParam("options", "list", "可选项列表", False),
    ], "ctrl")

    register("fail", "操作失败，报告错误原因", [
        ToolParam("reason", "str", "失败原因"),
    ], "ctrl")


# 模块加载时初始化
_init_tools()
