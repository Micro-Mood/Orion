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
from typing import Any, Dict, List, Optional, Tuple

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


def _write_index_entry(cwd: str, dir_name: str, entry: Dict[str, Any]) -> None:
    entries = load_index(cwd, dir_name)
    entries.append(entry)
    try:
        _index_path(cwd, dir_name).write_text(
            json.dumps({"entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except IOError as e:
        logger.error(f"[memory] 写入索引失败: {e}")


def archive_markdown(
    cwd: str,
    title: str,
    markdown: str,
    dir_name: str = ".orion",
    sidecar: Optional[Dict[str, Any]] = None,
    index_extra: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """归档一段 markdown 记忆；可同时写入机器 sidecar。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    mem_dir = _memory_dir(cwd, dir_name)
    fname = f"{ts}.md"
    fpath = mem_dir / fname

    safe_title = (title or "历史归档").strip().replace("\n", " ")[:60]
    body = (markdown or "").strip() or "(无内容)"
    if body.startswith("#"):
        content = body
        if "**时间**" not in content[:300]:
            lines = content.splitlines()
            if lines:
                content = "\n".join([lines[0], "", f"**时间**: {ts}", *lines[1:]])
    else:
        content = f"# {safe_title}\n\n**时间**: {ts}\n\n{body}"

    try:
        fpath.write_text(content.rstrip() + "\n", encoding="utf-8")
    except IOError as e:
        logger.error(f"[memory] 写入记忆文件失败: {e}")
        raise

    rel = f"{dir_name}/{fname}"
    sidecar_rel: Optional[str] = None
    if sidecar is not None:
        sidecar_name = f"{ts}.ctx.json"
        sidecar_path = mem_dir / sidecar_name
        sidecar_data = dict(sidecar)
        sidecar_data.setdefault("id", ts)
        sidecar_data.setdefault("file", rel)
        sidecar_data.setdefault("created", datetime.now().isoformat(timespec="seconds"))
        try:
            sidecar_path.write_text(
                json.dumps(sidecar_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            sidecar_rel = f"{dir_name}/{sidecar_name}"
        except IOError as e:
            logger.error(f"[memory] 写入记忆 sidecar 失败: {e}")
            raise

    entry: Dict[str, Any] = {
        "id": ts,
        "title": safe_title,
        "file": rel,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    if sidecar_rel:
        entry["sidecar"] = sidecar_rel
    if index_extra:
        entry.update(index_extra)
    _write_index_entry(cwd, dir_name, entry)

    return rel, sidecar_rel


def archive_memory(
    cwd: str,
    title: str,
    summary: str,
    user_quotes: str = "",
    llm_notes: str = "",
    dir_name: str = ".orion",
) -> str:
    """归档一段记忆为 markdown 文件并写入索引。返回相对路径 (.orion/xxx.md)。"""
    safe_title = (title or "未命名记忆").strip().replace("\n", " ")[:60]
    content = f"## 对话摘要\n{summary or '(无)'}\n"
    if user_quotes:
        content += f"\n## 用户原话摘录\n{user_quotes}\n"
    if llm_notes:
        content += f"\n## LLM 评论\n{llm_notes}\n"
    rel, _ = archive_markdown(cwd, safe_title, content, dir_name=dir_name)
    return rel


def build_memory_section(cwd: str, dir_name: str = ".orion") -> str:
    """便捷方法: 读取索引并格式化为 prompt 文本。"""
    return format_index_for_prompt(load_index(cwd, dir_name), dir_name)
