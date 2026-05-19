"""
Orion 提示词管理
================

加载模板、注入工具目录、TTL 和工作目录，生成系统提示。
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path

from tools import get_tool_catalog

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_template() -> str:
    """加载提示词模板"""
    template_file = PROMPT_DIR / "system.md"
    if template_file.exists():
        return template_file.read_text(encoding="utf-8")
    # Fallback
    return (
        "You are Orion. Working dir: {cwd}. "
        "Register tools via register_tool before use. "
        "Catalog:\n{tool_catalog}"
    )


def build_system_prompt(cwd: str, ttl_rounds: int = 5) -> str:
    """构建完整的系统提示。"""
    now = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M %A")
    template = _load_template()
    replacements = {
        "{datetime}": now,
        "{cwd}": cwd,
        "{tool_catalog}": get_tool_catalog(),
        "{ttl_rounds}": str(ttl_rounds),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template
