"""
Orion 长期记忆管理
==================

将旧对话压缩归档到 <cwd>/.orion/<id>.md，并维护索引 <cwd>/.orion/index.json。
索引在 system prompt 中暴露给 AI；AI 自行决定何时 read_file 加载具体记忆。

设计原则:
- 索引轻量(只有标题+路径), 按需懒加载, 避免长期记忆膨胀上下文。
- 记忆文件用 markdown, 包含摘要 / 用户原话 / LLM 评论, 便于人工查看。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _memory_dir(cwd: str, name: str = ".orion") -> Path:
    p = Path(cwd) / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _index_path(cwd: str, name: str = ".orion") -> Path:
    return _memory_dir(cwd, name) / "index.json"


def load_index(cwd: str, name: str = ".orion") -> List[Dict]:
    """读取记忆索引；不存在或损坏则返回空列表。"""
    p = _index_path(cwd, name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries", [])
        if isinstance(entries, list):
            return entries
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"[memory] 读取索引失败: {e}")
    return []


def format_index_for_prompt(entries: List[Dict], dir_name: str = ".orion") -> str:
    """把索引格式化成 system prompt 里的文本块。无条目则返回空串。"""
    if not entries:
        return ""
    lines = [
        "## 长期记忆索引",
        f"以下是过往会话已归档的记忆条目 (位于 `{dir_name}/`)。",
        "如与当前任务相关, 主动 `register_tool([\"read_file\"])` 后 `read_file` 加载完整内容。",
        "",
    ]
    for e in entries:
        title = e.get("title", "(无标题)")
        f = e.get("file", "")
        created = e.get("created", "")
        lines.append(f"- **{title}** — `{f}` ({created})")
    return "\n".join(lines)


def archive_memory(
    cwd: str,
    title: str,
    summary: str,
    user_quotes: str = "",
    llm_notes: str = "",
    dir_name: str = ".orion",
) -> str:
    """归档一段记忆为 markdown 文件并写入索引。返回相对路径 (.orion/xxx.md)。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    mem_dir = _memory_dir(cwd, dir_name)
    fname = f"{ts}.md"
    fpath = mem_dir / fname

    safe_title = (title or "未命名记忆").strip().replace("\n", " ")[:60]

    content = f"# {safe_title}\n\n**时间**: {ts}\n\n## 对话摘要\n{summary or '(无)'}\n"
    if user_quotes:
        content += f"\n## 用户原话摘录\n{user_quotes}\n"
    if llm_notes:
        content += f"\n## LLM 评论\n{llm_notes}\n"

    try:
        fpath.write_text(content, encoding="utf-8")
    except IOError as e:
        logger.error(f"[memory] 写入记忆文件失败: {e}")
        raise

    rel = f"{dir_name}/{fname}"
    entries = load_index(cwd, dir_name)
    entries.append({
        "id": ts,
        "title": safe_title,
        "file": rel,
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        _index_path(cwd, dir_name).write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except IOError as e:
        logger.error(f"[memory] 写入索引失败: {e}")

    return rel


def build_memory_section(cwd: str, dir_name: str = ".orion") -> str:
    """便捷方法: 读取索引并格式化为 prompt 文本。"""
    return format_index_for_prompt(load_index(cwd, dir_name), dir_name)
