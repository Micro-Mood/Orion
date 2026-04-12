"""
Orion 提示词管理
================

加载模板、注入工具列表和工作目录，生成系统提示。
"""

from datetime import datetime
from pathlib import Path

from tools import get_names_by_category

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_template() -> str:
    """加载提示词模板"""
    template_file = PROMPT_DIR / "system.md"
    if template_file.exists():
        return template_file.read_text(encoding="utf-8")

    # 备用模板
    return (
        "你是 Orion，个人 AI 助手。\n\n"
        "## 工作目录\n{cwd}\n\n"
        "## 可用工具\n{tool_list}\n\n"
        "分析需求后选择工具: {{\"select\": [\"工具名\"]}}\n"
        "调用工具: {{\"call\": \"工具名\", \"参数\": \"值\"}}\n"
        "完成: {{\"call\": \"done\", \"summary\": \"摘要\"}}\n"
        "提问: {{\"call\": \"ask\", \"question\": \"问题\"}}\n"
    )


def build_system_prompt(cwd: str) -> str:
    """
    构建完整的系统提示
    
    Args:
        cwd: 当前工作目录
        
    Returns:
        完整的系统提示文本
    """
    # 生成按分类的工具名列表
    categories = get_names_by_category()
    lines = []
    for cat, names in categories.items():
        if cat == "ctrl":
            continue  # 控制指令在模板中单独说明
        lines.append(f"- {cat}: {', '.join(names)}")
    tool_list = "\n".join(lines)

    # 注入变量
    now = datetime.now().strftime("%Y-%m-%d %H:%M %A")
    template = _load_template()
    prompt = (template
              .replace("{cwd}", cwd)
              .replace("{tool_list}", tool_list)
              .replace("{datetime}", now))
    return prompt
