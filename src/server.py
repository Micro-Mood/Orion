"""Orion — FastAPI + WebSocket Server"""

import json
import uuid
import random
import asyncio
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Orion")

# ============================================
# 数据存储（内存，后续替换为持久化）
# ============================================
sessions: dict = {}        # session_id -> {id, title, created_at, updated_at}
session_messages: dict = {} # session_id -> [messages]
connections: list = []      # active WebSocket connections


def new_session():
    sid = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    s = {"id": sid, "title": "新对话", "created_at": now, "updated_at": now}
    sessions[sid] = s
    session_messages[sid] = []
    return s


# ============================================
# WebSocket
# ============================================
async def broadcast(data: dict, exclude=None):
    msg = json.dumps(data, ensure_ascii=False)
    for ws in connections:
        if ws is not exclude:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connections.append(ws)
    try:
        while True:
            text = await ws.receive_text()
            data = json.loads(text)
            await handle_ws_message(ws, data)
    except WebSocketDisconnect:
        pass
    finally:
        connections.remove(ws)


async def handle_ws_message(ws: WebSocket, data: dict):
    msg_type = data.get("type")

    if msg_type == "get_sessions":
        sorted_sessions = sorted(sessions.values(), key=lambda s: s["updated_at"], reverse=True)
        await ws.send_text(json.dumps({
            "type": "session_list",
            "sessions": sorted_sessions
        }, ensure_ascii=False))

    elif msg_type == "create_session":
        s = new_session()
        await broadcast({"type": "session_created", "session": s})

    elif msg_type == "delete_session":
        sid = data.get("session_id")
        sessions.pop(sid, None)
        session_messages.pop(sid, None)
        await broadcast({"type": "session_deleted", "session_id": sid})

    elif msg_type == "get_messages":
        sid = data.get("session_id")
        msgs = session_messages.get(sid, [])
        await ws.send_text(json.dumps({
            "type": "session_messages",
            "session_id": sid,
            "messages": msgs
        }, ensure_ascii=False))

    elif msg_type == "send_message":
        sid = data.get("session_id")
        content = data.get("content", "")
        if sid not in sessions:
            return

        # 保存用户消息
        user_msg = {
            "id": f"user_{uuid.uuid4().hex[:8]}",
            "role": "user",
            "content": content
        }
        session_messages.setdefault(sid, []).append(user_msg)
        sessions[sid]["updated_at"] = datetime.now().isoformat()

        # 触发 AI 回复（目前是 mock，后续接入 engine）
        await mock_ai_reply(ws, sid, content)

    elif msg_type == "update_session_title":
        sid = data.get("session_id")
        title = data.get("title", "")
        if sid in sessions:
            sessions[sid]["title"] = title
            await broadcast({
                "type": "session_title_updated",
                "session_id": sid,
                "title": title
            })


async def mock_ai_reply(ws: WebSocket, session_id: str, user_content: str):
    """Mock AI 回复，用于前端调试。后续替换为真实 engine。"""
    msg_id = f"ai_{uuid.uuid4().hex[:8]}"

    async def send(data):
        await ws.send_text(json.dumps(data, ensure_ascii=False))

    async def tool(name, params, result, success=True, duration=None):
        """模拟工具调用：start → 延时 → end"""
        await send({"type": "tool_start", "session_id": session_id,
                     "tool_name": name, "params": params})
        d = duration or random.randint(15, 80)
        await asyncio.sleep(d / 200)  # 缩短等待让演示更快
        await send({"type": "tool_end", "session_id": session_id,
                     "tool_name": name, "success": success,
                     "result": result, "duration": d})
        await asyncio.sleep(0.1)

    # 1. message_start
    await send({"type": "message_start", "session_id": session_id,
                "message_id": msg_id})
    await asyncio.sleep(0.2)

    # 2. 根据关键词触发不同的工具 demo
    content_lower = user_content.lower()

    if "demo" in content_lower or "工具" in content_lower or "展示" in content_lower:
        # ===== 完整 Demo：演示所有工具类型 =====
        await tool("grep_search",
                    {"query": "记账", "path": "/workspace"},
                    "在 3 个文件中找到 7 处匹配:\n  notes/2024.md: 3处\n  notes/budget.md: 2处\n  data/records.json: 2处",
                    duration=52)

        await tool("read_file",
                    {"path": "/workspace/notes/2024.md", "startLine": 1, "endLine": 25},
                    "# 2024 生活笔记\n\n## 1月\n- 键盘 Cherry MX ¥320\n- 鼠标垫 ¥45\n- 显示器支架 ¥189\n\n## 2月\n- 理发 ¥35\n- 书《深入理解计算机系统》¥79",
                    duration=18)

        await tool("edit_file",
                    {"filePath": "/workspace/notes/2024.md", "startLine": 12, "endLine": 15},
                    "已更新 3 行内容",
                    duration=23)

        await tool("create_file",
                    {"path": "/workspace/notes/summary.md"},
                    "文件已创建",
                    duration=8)

        await tool("list_dir",
                    {"path": "/workspace/notes"},
                    "2024.md\nbudget.md\nsummary.md\ntodo.txt\narchive/",
                    duration=6)

        await tool("file_search",
                    {"query": "*.json"},
                    "找到 4 个文件:\n  data/records.json\n  config/settings.json\n  package.json\n  tsconfig.json",
                    duration=35)

        await tool("run_in_terminal",
                    {"command": "python scripts/calc_total.py --month 2024-01"},
                    "$ python scripts/calc_total.py --month 2024-01\n\n2024年1月总支出: ¥554.00\n  日用品: ¥365.00\n  食品: ¥189.00\n\n完成",
                    duration=120)

        await tool("semantic_search",
                    {"query": "上个月的预算计划"},
                    "找到 2 条相关内容:\n  [0.92] notes/budget.md:15 \"3月预算计划：日用品500，食品800...\"\n  [0.85] notes/2024.md:28 \"3月总结：实际超支120元\"",
                    duration=68)

        await tool("delete_file",
                    {"path": "/workspace/notes/temp_draft.md"},
                    "文件已删除",
                    duration=5)

        reply = "以上展示了 Orion 的 **9 种工具调用**：\n\n" \
                "1. **grep_search** — 在工作区内搜索关键词\n" \
                "2. **read_file** — 读取文件内容（支持行范围）\n" \
                "3. **edit_file** — 编辑文件指定行\n" \
                "4. **create_file** — 创建新文件\n" \
                "5. **list_dir** — 列出目录内容\n" \
                "6. **file_search** — 按模式查找文件\n" \
                "7. **run_in_terminal** — 执行终端命令\n" \
                "8. **semantic_search** — 语义搜索\n" \
                "9. **delete_file** — 删除文件\n\n" \
                "点击每项可以展开查看详情。"

    elif "文件" in user_content or "笔记" in user_content or "记" in user_content:
        await tool("grep_search",
                    {"query": user_content[:10], "path": "/workspace"},
                    f"在 2 个文件中找到 3 处匹配",
                    duration=45)
        await tool("read_file",
                    {"path": "/workspace/notes/2024.md"},
                    "# 2024 笔记\n- 键盘 300 元\n- 鼠标 150 元\n- 显示器支架 189 元",
                    duration=12)
        reply = f"找到了相关内容。你的笔记中记录了：\n\n- 键盘 300 元\n- 鼠标 150 元\n- 显示器支架 189 元\n\n共计 **639 元**。"

    elif "运行" in user_content or "命令" in user_content or "终端" in user_content:
        await tool("run_in_terminal",
                    {"command": "echo 'Hello from Orion!'"},
                    "$ echo 'Hello from Orion!'\nHello from Orion!",
                    duration=35)
        reply = "命令已执行完成。"

    elif "搜索" in user_content:
        await tool("grep_search",
                    {"query": user_content.replace("搜索", "").strip(), "path": "/workspace"},
                    "在 5 个文件中找到 12 处匹配",
                    duration=62)
        reply = f"搜索完成，共找到 **12 处**匹配。"

    elif "编辑" in user_content or "修改" in user_content:
        await tool("read_file",
                    {"path": "/workspace/config.yaml", "startLine": 1, "endLine": 10},
                    "# Orion Config\nmodel: qwen-flash\nmax_tokens: 4096\ntemperature: 0.7",
                    duration=10)
        await tool("edit_file",
                    {"filePath": "/workspace/config.yaml", "startLine": 3, "endLine": 3},
                    "已将 max_tokens 从 4096 更新为 8192",
                    duration=15)
        reply = "配置已更新：`max_tokens` 从 4096 改为 **8192**。"

    else:
        reply = f"收到你的消息：「{user_content}」\n\n这是 Orion 的 **mock 回复**。\n\n💡 输入 **demo** 或 **展示工具** 可以查看所有工具调用的演示效果。"

    # 3. 流式回复
    for chunk in split_text(reply, 4):
        await send({"type": "message_delta", "session_id": session_id,
                     "content": chunk})
        await asyncio.sleep(0.03)

    # 4. message_end + 保存 + done
    await send({"type": "message_end", "session_id": session_id,
                "message_id": msg_id, "content": reply})

    session_messages[session_id].append({
        "id": msg_id, "role": "assistant", "content": reply,
        "tool_calls": []  # mock 不保存工具调用详情到存储
    })

    await send({"type": "done", "session_id": session_id})


def split_text(text, chunk_size):
    """将文本拆成小块，模拟流式输出"""
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


# ============================================
# 静态文件
# ============================================
@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
