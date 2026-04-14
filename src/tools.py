"""
Orion 工具注册表
================

Registers Axon MCP Server's 27 tool methods + control instructions.
Compact description format: SELECT phase sends tool names only, PARAMS phase sends compact desc to save tokens.
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
        params_str = ";" .join(parts) if parts else "(no params)"
        return f"{self.name}|{self.desc}|{params_str}"


# ==================== 全局注册表 ====================

TOOLS: Dict[str, Tool] = {}


def register(name: str, desc: str, params: List[ToolParam], category: str):
    """注册工具"""
    TOOLS[name] = Tool(name=name, desc=desc, params=params, category=category)


def get_tool(name: str) -> Optional[Tool]:
    """获取工具定义"""
    return TOOLS.get(name)


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
    """Register all tools (matching Axon MCP Server's 27 methods + control instructions)"""

    # ==================== File Operations (file) — 12 ====================

    register("read_file", "Read file content", [
        ToolParam("path", "str", "File path"),
        ToolParam("encoding", "str", "Encoding", False, "utf-8"),
        ToolParam("line_range", "list", "Line range [start, end]", False),
        ToolParam("max_size", "int", "Max bytes", False),
    ], "file")

    register("write_file", "Write file (create if not exists)", [
        ToolParam("path", "str", "File path"),
        ToolParam("content", "str", "File content"),
        ToolParam("encoding", "str", "Encoding", False, "utf-8"),
    ], "file")

    register("stat_path", "Get file/directory status info", [
        ToolParam("path", "str", "Path"),
        ToolParam("follow_symlinks", "bool", "Follow symlinks", False, "true"),
    ], "file")

    register("list_directory", "List directory contents", [
        ToolParam("path", "str", "Directory path"),
        ToolParam("pattern", "str", "Glob pattern", False),
        ToolParam("recursive", "bool", "Recursive", False, "false"),
        ToolParam("include_hidden", "bool", "Include hidden files", False, "false"),
        ToolParam("max_results", "int", "Max results", False),
    ], "file")

    register("delete_file", "Delete a file", [
        ToolParam("path", "str", "File path"),
    ], "file")

    register("delete_directory", "Delete a directory", [
        ToolParam("path", "str", "Directory path"),
        ToolParam("recursive", "bool", "Recursive delete", False, "false"),
        ToolParam("force", "bool", "Force delete", False, "false"),
    ], "file")

    register("move_file", "Move/rename a file", [
        ToolParam("source", "str", "Source path"),
        ToolParam("dest", "str", "Destination path"),
        ToolParam("overwrite", "bool", "Overwrite", False, "false"),
    ], "file")

    register("copy_file", "Copy a file", [
        ToolParam("source", "str", "Source path"),
        ToolParam("dest", "str", "Destination path"),
        ToolParam("overwrite", "bool", "Overwrite", False, "false"),
    ], "file")

    register("create_directory", "Create a directory", [
        ToolParam("path", "str", "Directory path"),
        ToolParam("recursive", "bool", "Create parents", False, "true"),
    ], "file")

    register("replace_string_in_file", "Text match & replace (old_string must be unique)", [
        ToolParam("path", "str", "File path"),
        ToolParam("old_string", "str", "Original text to replace"),
        ToolParam("new_string", "str", "New text to replace with"),
        ToolParam("encoding", "str", "Encoding", False, "utf-8"),
    ], "file")

    register("multi_replace_string_in_file", "Batch text replacements (each old_string must be unique)", [
        ToolParam("replacements", "list", "List of {path, old_string, new_string}"),
        ToolParam("encoding", "str", "Encoding", False, "utf-8"),
    ], "file")

    register("move_directory", "Move/rename a directory", [
        ToolParam("source", "str", "Source path"),
        ToolParam("dest", "str", "Destination path"),
    ], "file")

    # ==================== Search Operations (search) — 3 ====================

    register("find_files", "Search files by pattern", [
        ToolParam("pattern", "str", "Glob pattern"),
        ToolParam("root", "str", "Search root directory", False),
        ToolParam("recursive", "bool", "Recursive", False, "true"),
        ToolParam("file_types", "list", "File type filter", False),
        ToolParam("include_hidden", "bool", "Include hidden files", False, "false"),
        ToolParam("max_results", "int", "Max results", False),
    ], "search")

    register("search_text", "Search text content in files", [
        ToolParam("query", "str", "Search query"),
        ToolParam("root", "str", "Search root directory", False),
        ToolParam("file_pattern", "str", "File glob pattern", False, "*"),
        ToolParam("case_sensitive", "bool", "Case sensitive", False, "false"),
        ToolParam("is_regex", "bool", "Regex mode", False, "false"),
        ToolParam("context_lines", "int", "Context lines", False, "2"),
        ToolParam("include_hidden", "bool", "Include hidden files", False, "false"),
        ToolParam("max_results", "int", "Max results", False),
    ], "search")

    register("find_symbol", "Search code symbols (functions/classes/variables)", [
        ToolParam("symbol", "str", "Symbol name"),
        ToolParam("root", "str", "Search root directory", False),
        ToolParam("symbol_type", "str", "Symbol type", False),
        ToolParam("file_pattern", "str", "File glob pattern", False, "*"),
        ToolParam("include_hidden", "bool", "Include hidden files", False, "false"),
        ToolParam("max_results", "int", "Max results", False),
    ], "search")

    # ==================== Command Execution (command) — 10 ====================

    register("run_command", "Run command and wait for completion", [
        ToolParam("command", "str", "Command string"),
        ToolParam("cwd", "str", "Working directory", False),
        ToolParam("timeout", "int", "Timeout in ms", False),
        ToolParam("env", "dict", "Environment variables", False),
    ], "command")

    register("create_task", "Create a background process", [
        ToolParam("command", "str", "Command string"),
        ToolParam("cwd", "str", "Working directory", False),
        ToolParam("timeout", "int", "Timeout in ms", False),
        ToolParam("env", "dict", "Environment variables", False),
    ], "command")

    register("stop_task", "Stop a process", [
        ToolParam("task_id", "str", "Task ID"),
        ToolParam("force", "bool", "Force kill", False, "false"),
    ], "command")

    register("wait_task", "Wait for process completion", [
        ToolParam("task_id", "str", "Task ID"),
        ToolParam("timeout", "int", "Timeout in ms", False),
    ], "command")

    register("task_status", "Query process status", [
        ToolParam("task_id", "str", "Task ID"),
    ], "command")

    register("read_stdout", "Read process stdout", [
        ToolParam("task_id", "str", "Task ID"),
        ToolParam("max_chars", "int", "Max characters", False, "8192"),
    ], "command")

    register("read_stderr", "Read process stderr", [
        ToolParam("task_id", "str", "Task ID"),
        ToolParam("max_chars", "int", "Max characters", False, "8192"),
    ], "command")

    register("write_stdin", "Write to process stdin", [
        ToolParam("task_id", "str", "Task ID"),
        ToolParam("data", "str", "Input data"),
        ToolParam("eof", "bool", "Send EOF", False, "false"),
    ], "command")

    register("list_tasks", "List all processes", [], "command")

    register("del_task", "Delete a finished process", [
        ToolParam("task_id", "str", "Task ID"),
    ], "command")

    # ==================== System Info (system) — 1 ====================

    register("get_system_info", "Get system information", [], "system")

    # ==================== Web (web) — 1 ====================

    register("fetch_webpage", "Fetch web page content", [
        ToolParam("url", "str", "Web page URL"),
        ToolParam("query", "str", "Search keyword", False),
    ], "web")

    # ==================== Control Instructions (ctrl) — 4 ====================

    register("done", "Finish reply, end current turn", [
        ToolParam("summary", "str", "Reply summary"),
    ], "ctrl")

    register("ask", "Ask user a question, wait for answer", [
        ToolParam("question", "str", "Question"),
        ToolParam("options", "list", "Options list", False),
    ], "ctrl")

    register("fail", "Report operation failure", [
        ToolParam("reason", "str", "Failure reason"),
    ], "ctrl")

    register("set_session_title", "Set current session title (use when topic is clear)", [
        ToolParam("title", "str", "Session title (concise, ≤20 chars)"),
    ], "ctrl")


# 模块加载时初始化
_init_tools()
