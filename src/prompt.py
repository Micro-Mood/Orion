"""
Orion 提示词管理
================

加载模板、注入工具列表和工作目录，生成系统提示。
"""

from datetime import datetime
from pathlib import Path

from tools import get_names_by_category, get_tool

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_template() -> str:
    """加载提示词模板"""
    template_file = PROMPT_DIR / "system.md"
    if template_file.exists():
        return template_file.read_text(encoding="utf-8")

    # Fallback template
    return (
        "You are Orion, a personal AI assistant.\n\n"
        "## Working Directory\n{cwd}\n\n"
        "## Available Tools\n{tool_list}\n\n"
        "Select tools: {{\"select\": [\"tool_name\"]}}\n"
        "Call tool: {{\"call\": \"tool_name\", \"param\": \"value\"}}\n"
        "Done: {{\"call\": \"done\", \"summary\": \"summary\"}}\n"
        "Ask: {{\"call\": \"ask\", \"question\": \"question\"}}\n"
    )


def build_system_prompt(cwd: str) -> str:
    """
    构建完整的系统提示
    
    Args:
        cwd: 当前工作目录
        
    Returns:
        完整的系统提示文本
    """
    # 生成按分类的工具名列表（带一句话描述）
    categories = get_names_by_category()
    lines = []
    for cat, names in categories.items():
        if cat == "ctrl":
            continue  # 控制指令在模板中单独说明
        items = []
        for n in names:
            tool = get_tool(n)
            items.append(f"{n}({tool.desc})" if tool else n)
        lines.append(f"- {cat}: {', '.join(items)}")
    tool_list = "\n".join(lines)

    # 注入变量（单次替换，避免链式替换中 cwd 包含模板标记被误换）
    now = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    template = _load_template()
    replacements = {
        "{datetime}": now,
        "{cwd}": cwd,
        "{tool_list}": tool_list,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template
