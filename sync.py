"""
sync.py — Orion ↔ cocandy_server 双向同步

用法:
  python sync.py pull   # cocandy_server → tests/Orion (取最新部署改动)
  python sync.py push   # tests/Orion → cocandy_server (把上游改动写入部署仓库)
  python sync.py diff   # 列出两边不一致的文件

原则:
  - 只同步 src/ 和 axon/src/  (不动 config.json / data/ / workspace/ / venv / __pycache__)
  - .git/ 永远不碰
  - axon/ 只同步 src/ 子目录 (axon/.git 是独立仓库，不动)
"""

import sys
import os
import shutil
import filecmp
from pathlib import Path

# ── 路径配置 ────────────────────────────────────────────────────
THIS_DIR   = Path(__file__).resolve().parent                        # tests/Orion
SERVER_DIR = THIS_DIR.parents[1] / "Server" / "services" / "orion" / "api"
                                                                    # Server/services/orion/api

# 需要同步的顶层条目（相对于两个根）
# "src"         → 整个 src/ 目录（含 prompts/ web/）
# "axon/src"    → axon 的源码（跳过 axon/.git）
# "requirements.txt", ".gitignore" 等单文件
SYNC_DIRS  = ["src", "axon/src"]
SYNC_FILES = ["requirements.txt", ".gitignore"]

# 永远排除的目录/文件名（任意层级）
EXCLUDE_NAMES = {
    ".git", "__pycache__", "venv", ".venv",
    "data", "workspace",
    "config.json",      # 含密钥，不同步
    "*.pyc",
}


def _should_skip(path: Path) -> bool:
    for part in path.parts:
        if part in EXCLUDE_NAMES:
            return True
        if part.startswith(".") and part != ".gitignore":
            return True
    return False


def _copy_tree(src: Path, dst: Path, dry: bool = False) -> list[str]:
    """把 src 目录递归覆盖到 dst，返回已复制的文件列表。"""
    changed = []
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if _should_skip(rel):
            continue
        target = dst / rel
        if item.is_dir():
            if not dry:
                target.mkdir(parents=True, exist_ok=True)
            continue
        # 文件
        if target.exists() and filecmp.cmp(str(item), str(target), shallow=False):
            continue  # 内容相同，跳过
        changed.append(str(rel))
        if not dry:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(target))
    return changed


def _diff_tree(a: Path, b: Path) -> list[str]:
    """列出 a 和 b 之间内容不同的文件（相对路径）。"""
    changed = []
    seen = set()

    for item in a.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(a)
        if _should_skip(rel):
            continue
        seen.add(rel)
        other = b / rel
        if not other.exists():
            changed.append(f"ONLY_IN_A  {rel}")
        elif not filecmp.cmp(str(item), str(other), shallow=False):
            changed.append(f"DIFFER     {rel}")

    for item in b.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(b)
        if _should_skip(rel) or rel in seen:
            continue
        changed.append(f"ONLY_IN_B  {rel}")

    return sorted(changed)


def cmd_diff():
    print(f"A = {THIS_DIR}")
    print(f"B = {SERVER_DIR}")
    diffs = []
    for d in SYNC_DIRS:
        a, b = THIS_DIR / d, SERVER_DIR / d
        if a.exists() and b.exists():
            for line in _diff_tree(a, b):
                diffs.append(f"  [{d}] {line}")
    for f in SYNC_FILES:
        a, b = THIS_DIR / f, SERVER_DIR / f
        if a.exists() and b.exists():
            if not filecmp.cmp(str(a), str(b), shallow=False):
                diffs.append(f"  DIFFER     {f}")
    if diffs:
        print("\n".join(diffs))
    else:
        print("两边完全一致。")


def cmd_pull():
    """cocandy_server → tests/Orion（把部署仓库里的改动拉回来）"""
    print(f"PULL: {SERVER_DIR}  →  {THIS_DIR}")
    total = []
    for d in SYNC_DIRS:
        src, dst = SERVER_DIR / d, THIS_DIR / d
        if not src.exists():
            print(f"  跳过（不存在）: {d}")
            continue
        changed = _copy_tree(src, dst)
        total.extend([f"[{d}] {f}" for f in changed])
    for f in SYNC_FILES:
        src, dst = SERVER_DIR / f, THIS_DIR / f
        if not src.exists():
            continue
        if not dst.exists() or not filecmp.cmp(str(src), str(dst), shallow=False):
            shutil.copy2(str(src), str(dst))
            total.append(f"[root] {f}")
    if total:
        print(f"  已更新 {len(total)} 个文件:")
        for f in total:
            print(f"    {f}")
    else:
        print("  无变更。")


def cmd_push():
    """tests/Orion → cocandy_server（把上游改动写入部署仓库）"""
    print(f"PUSH: {THIS_DIR}  →  {SERVER_DIR}")
    total = []
    for d in SYNC_DIRS:
        src, dst = THIS_DIR / d, SERVER_DIR / d
        if not src.exists():
            print(f"  跳过（不存在）: {d}")
            continue
        changed = _copy_tree(src, dst)
        total.extend([f"[{d}] {f}" for f in changed])
    for f in SYNC_FILES:
        src, dst = THIS_DIR / f, SERVER_DIR / f
        if not src.exists():
            continue
        if not dst.exists() or not filecmp.cmp(str(src), str(dst), shallow=False):
            shutil.copy2(str(src), str(dst))
            total.append(f"[root] {f}")
    if total:
        print(f"  已更新 {len(total)} 个文件:")
        for f in total:
            print(f"    {f}")
    else:
        print("  无变更。")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("pull", "push", "diff"):
        print("用法: python sync.py [pull|push|diff]")
        sys.exit(1)
    {"pull": cmd_pull, "push": cmd_push, "diff": cmd_diff}[sys.argv[1]]()
