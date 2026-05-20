"""
Orion — FastAPI + WebSocket Server
===================================

WebSocket 消息协议:
  Client → Server: get_sessions, create_session, delete_session,
                   get_messages, send_message, update_session_title, cancel,
                   get_config, save_config, test_llm, test_axon
  Server → Client: session_list, session_created, session_deleted,
                   session_messages, message_start, message_delta,
                   message_end, tool_start, tool_end, done, ask, error,
                   session_title_updated, model_info, config_data,
                   config_saved, test_result
"""

import asyncio
import datetime
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import bcrypt
import jwt

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from config import get_config
from engine import EngineCallbacks, OrionEngine
from llm import LLMClient, LLMError
from mcp_client import MCPClient
from store import SessionStore

import axon_manager as _axon_mod

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


# ─────────────────────────────────────────────────────────
#  工具结果压缩 —— 让发给前端的 JSON 始终合法且不超过 ~8K
# ─────────────────────────────────────────────────────────
_BIG_TEXT_FIELDS = ("stdout", "stderr", "output", "content", "text", "body")
_BIG_LIST_FIELDS = ("matches", "entries", "tasks", "hits", "results", "items", "files")
_FIELD_TEXT_MAX = 4000     # 单个大文本字段上限
_LIST_ITEM_MAX = 100       # 大列表保留条数
_TOTAL_MAX = 8000          # 最终序列化总长上限（兜底硬截）


def _shrink_obj(obj):
    """递归压缩 dict/list：大文本截断、大列表截短，保留结构合法。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and k in _BIG_TEXT_FIELDS and len(v) > _FIELD_TEXT_MAX:
                out[k] = v[:_FIELD_TEXT_MAX] + f"\n... (truncated, original {len(v)} chars)"
            elif isinstance(v, list) and k in _BIG_LIST_FIELDS and len(v) > _LIST_ITEM_MAX:
                out[k] = [_shrink_obj(x) for x in v[:_LIST_ITEM_MAX]]
                out[f"_{k}_total"] = len(v)
            else:
                out[k] = _shrink_obj(v)
        return out
    if isinstance(obj, list):
        if len(obj) > _LIST_ITEM_MAX:
            return [_shrink_obj(x) for x in obj[:_LIST_ITEM_MAX]] + [
                {"_truncated": f"... +{len(obj) - _LIST_ITEM_MAX} more items"}
            ]
        return [_shrink_obj(x) for x in obj]
    return obj


def _compact_result(result) -> str:
    """
    把工具结果压成合法且不超长的 JSON 字符串。

    - 入参可以是 dict / list / 已 dump 的 JSON 字符串 / 普通字符串
    - 失败兜底：直接对字符串硬截到 _TOTAL_MAX
    """
    if result is None:
        return ""

    data = result
    # 若是 JSON 字符串先反序列化
    if isinstance(result, str):
        try:
            data = json.loads(result)
        except (ValueError, TypeError):
            return result[:_TOTAL_MAX]

    if not isinstance(data, (dict, list)):
        s = str(data)
        return s[:_TOTAL_MAX]

    shrunk = _shrink_obj(data)
    try:
        s = json.dumps(shrunk, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(result)[:_TOTAL_MAX]

    if len(s) <= _TOTAL_MAX:
        return s

    # 仍然超长：硬截但保持以 ... 收尾（前端 tryJson 兜底会回溯到最后合法位置）
    return s[:_TOTAL_MAX] + "\n... (truncated)"



@asynccontextmanager
async def _lifespan(application: FastAPI):
    """应用生命周期：启动/关闭"""
    yield
    # --- shutdown ---
    _stop_fs_watcher()
    if _llm:
        await _llm.close()
    if _mcp:
        await _mcp.disconnect()


app = FastAPI(title="Orion", lifespan=_lifespan)

# 认证状态
_setup_lock = asyncio.Lock()
_login_failures: dict = {}  # ip -> {count, locked_until}


# ============================================
# 全局状态
# ============================================
store = SessionStore()
connections: List[WebSocket] = []
# 每个会话当前正在处理的 task
active_tasks: Dict[str, asyncio.Task] = {}
# ws → set of session_ids（用于 WebSocket 断开时清理 task）
_ws_sessions: Dict[WebSocket, set] = {}
# session_id → 待确认工具调用状态
_pending_confirms: Dict[str, dict] = {}

# 延迟初始化
_engine: OrionEngine = None
_mcp: MCPClient = None
_llm: LLMClient = None

# 文件系统监控
_fs_observer: Optional[Observer] = None
_fs_loop: asyncio.AbstractEventLoop = None
_fs_pending: set = set()           # 待广播的路径
_fs_debounce_handle = None         # debounce 定时器


class _FSHandler(FileSystemEventHandler):
    """watchdog 事件 → asyncio 广播（带 debounce）"""

    def _schedule(self, path: str):
        global _fs_debounce_handle
        _fs_pending.add(path)
        # 取消上一次的定时器，重新等 300ms
        if _fs_debounce_handle:
            _fs_debounce_handle.cancel()
        _fs_debounce_handle = _fs_loop.call_later(0.3, _flush_fs_events)

    def on_created(self, event):
        _fs_loop.call_soon_threadsafe(self._schedule, event.src_path)

    def on_deleted(self, event):
        _fs_loop.call_soon_threadsafe(self._schedule, event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            _fs_loop.call_soon_threadsafe(self._schedule, event.src_path)

    def on_moved(self, event):
        _fs_loop.call_soon_threadsafe(self._schedule, event.src_path)
        _fs_loop.call_soon_threadsafe(self._schedule, event.dest_path)


def _flush_fs_events():
    """debounce 到期，统一广播一次"""
    global _fs_debounce_handle
    _fs_debounce_handle = None
    if not _fs_pending:
        return
    paths = list(_fs_pending)
    _fs_pending.clear()
    asyncio.ensure_future(_broadcast_fs(paths))


async def _broadcast_fs(paths: list):
    """向所有 WebSocket 客户端推送文件变化"""
    await broadcast({"type": "fs_changed", "paths": paths})


def _start_fs_watcher(watch_path: str):
    """启动文件系统监控"""
    global _fs_observer, _fs_loop
    _stop_fs_watcher()
    _fs_loop = asyncio.get_event_loop()
    _fs_observer = Observer()
    _fs_observer.schedule(_FSHandler(), watch_path, recursive=True)
    _fs_observer.daemon = True
    _fs_observer.start()
    logger.info(f"文件监控已启动: {watch_path}")


def _stop_fs_watcher():
    """停止文件系统监控"""
    global _fs_observer, _fs_debounce_handle
    if _fs_observer:
        _fs_observer.stop()
        _fs_observer.join(timeout=3)
        _fs_observer = None
    _fs_pending.clear()
    if _fs_debounce_handle:
        _fs_debounce_handle.cancel()
        _fs_debounce_handle = None


def _init_engine():
    """初始化引擎组件 (首次调用时)"""
    global _engine, _mcp, _llm

    if _engine is not None:
        return

    cfg = get_config()

    # API Key 允许为空，用户可在设置页配置
    _llm = LLMClient(
        api_key=cfg.llm.api_key or "placeholder",
        base_url=cfg.llm.base_url,
        models=cfg.llm.models,
        temperature=cfg.llm.temperature,
        timeout=cfg.llm.timeout,
        max_retries=cfg.llm.max_retries,
    )

    _mcp = MCPClient(
        host=cfg.axon.host,
        port=cfg.axon.port,
        connect_timeout=cfg.axon.connect_timeout,
        default_timeout=cfg.axon.call_timeout,
    )

    _engine = OrionEngine(
        llm=_llm,
        mcp=_mcp,
        store=store,
        max_iterations=cfg.engine.max_iterations,
        working_directory=cfg.get_working_directory(),
        read_file_max_lines=cfg.engine.read_file_max_lines,
        tool_ttl_rounds=cfg.engine.tool_ttl_rounds,
        context_window=cfg.engine.context_window,
        compress_at=cfg.engine.compress_at,
        context_recent_n=cfg.engine.context_recent_n,
        memory_dir=cfg.engine.memory_dir,
    )

    # 启动文件系统监控
    cwd = cfg.get_working_directory()
    if cwd and Path(cwd).is_dir():
        _start_fs_watcher(cwd)


def _get_axon_manager():
    """获取 main.py 传入的 AxonManager 实例"""
    return getattr(_axon_mod, '_instance', None)


async def _reinit_components():
    """重载配置并更新运行时组件"""
    cfg = get_config()

    if _llm:
        _llm.update_config(
            api_key=cfg.llm.api_key,
            base_url=cfg.llm.base_url,
            models=cfg.llm.models,
            temperature=cfg.llm.temperature,
        )

    if _mcp:
        if _mcp.host != cfg.axon.host or _mcp.port != cfg.axon.port:
            await _mcp.disconnect()
            _mcp.host = cfg.axon.host
            _mcp.port = cfg.axon.port

    if _engine:
        _engine.max_iterations = cfg.engine.max_iterations
        _engine.read_file_max_lines = cfg.engine.read_file_max_lines
        _engine.tool_ttl_rounds = cfg.engine.tool_ttl_rounds
        _engine.cwd = cfg.get_working_directory()
        _engine.context_window = cfg.engine.context_window
        _engine.compress_at = cfg.engine.compress_at
        _engine.context_recent_n = max(1, cfg.engine.context_recent_n)
        _engine.memory_dir = cfg.engine.memory_dir or ".orion"

    # 同步 AxonManager 配置
    axon_mgr = _get_axon_manager()
    if axon_mgr:
        axon_mgr.update_config(
            host=cfg.axon.host,
            port=cfg.axon.port,
            workspace=cfg.axon.workspace or cfg.get_working_directory(),
        )


# ============================================
# WebSocket 通信
# ============================================

async def send_to(ws: WebSocket, data: dict):
    """向单个客户端发送消息"""
    try:
        await ws.send_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.debug("WebSocket 发送失败，客户端可能已断开")


async def broadcast(data: dict, exclude: WebSocket = None):
    """广播消息到所有连接"""
    for ws in list(connections):
        if ws is not exclude:
            await send_to(ws, data)


# ============================================
# 认证
# ============================================

def _verify_token(token: str) -> bool:
    """验证 JWT token"""
    cfg = get_config()
    try:
        jwt.decode(token, cfg.auth.jwt_secret, algorithms=["HS256"])
        return True
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return False


def _create_token() -> str:
    """创建 JWT token"""
    cfg = get_config()
    payload = {
        "jti": uuid.uuid4().hex,
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(hours=cfg.auth.token_expiry_hours),
        "iat": datetime.datetime.now(datetime.timezone.utc),
    }
    return jwt.encode(payload, cfg.auth.jwt_secret, algorithm="HS256")


@app.post("/api/setup")
async def auth_setup(request: Request):
    """首次设置密码"""
    async with _setup_lock:
        cfg = get_config()
        if cfg.auth.password_hash:
            return JSONResponse({"error": "密码已设置"}, status_code=400)
        body = await request.json()
        password = body.get("password", "")
        if not password or len(password) < 6:
            return JSONResponse({"error": "密码至少6位"}, status_code=400)
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cfg.set_password_hash(hashed)
        cfg.save()
        return {"token": _create_token()}


@app.post("/api/login")
async def auth_login(request: Request):
    """登录"""
    ip = request.client.host if request.client else "unknown"
    now = time.time()

    # 清理过期的登录失败记录（防止内存无限增长）
    expired = [k for k, v in _login_failures.items()
               if v.get("locked_until", 0)
               and v["locked_until"] < now - 60]
    for k in expired:
        del _login_failures[k]

    # 速率限制: 5次失败后锁定60秒
    fail_info = _login_failures.get(ip, {})
    if fail_info.get("locked_until", 0) > now:
        wait = int(fail_info["locked_until"] - now)
        return JSONResponse({"error": f"尝试次数过多，请{wait}秒后重试"}, status_code=429)

    cfg = get_config()
    if not cfg.auth.password_hash:
        return JSONResponse({"error": "请先设置密码"}, status_code=400)
    body = await request.json()
    password = body.get("password", "")
    if not bcrypt.checkpw(password.encode(), cfg.auth.password_hash.encode()):
        count = fail_info.get("count", 0) + 1
        locked = now + 60 if count >= 5 else 0
        _login_failures[ip] = {"count": count, "locked_until": locked}
        return JSONResponse({"error": "密码错误"}, status_code=401)

    _login_failures.pop(ip, None)
    return {"token": _create_token()}


@app.post("/api/verify")
async def auth_verify(request: Request):
    """验证 token 是否有效"""
    body = await request.json()
    token = body.get("token", "")
    if _verify_token(token):
        return {"valid": True}
    return JSONResponse({"valid": False}, status_code=401)


@app.get("/__auth_status")
async def auth_status():
    """返回认证状态（是否需要设置密码）"""
    cfg = get_config()
    return {"needs_setup": not bool(cfg.auth.password_hash)}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # 首条消息认证（避免 token 暴露在 URL query 参数中）
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        data = json.loads(raw)
        if data.get("type") != "auth" or not _verify_token(data.get("token", "")):
            await send_to(ws, {"type": "auth_fail"})
            await ws.close(code=4001, reason="未授权")
            return
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await ws.close(code=4001, reason="未授权")
        return

    await send_to(ws, {"type": "auth_ok"})
    connections.append(ws)
    _ws_sessions[ws] = set()
    logger.info(f"WebSocket 连接: 当前 {len(connections)} 个")
    try:
        while True:
            text = await ws.receive_text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                await send_to(ws, {"type": "error", "message": "无效的 JSON"})
                continue
            await handle_ws_message(ws, data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket 异常: {e}")
    finally:
        if ws in connections:
            connections.remove(ws)
        # 任务继续在后台运行（不取消），结果保存到 DB
        _ws_sessions.pop(ws, None)
        logger.info(f"WebSocket 断开: 当前 {len(connections)} 个")


async def handle_ws_message(ws: WebSocket, data: dict):
    """分发 WebSocket 消息"""
    msg_type = data.get("type", "")
    handler = MESSAGE_HANDLERS.get(msg_type)

    if handler:
        try:
            await handler(ws, data)
        except Exception as e:
            logger.error(f"处理消息 {msg_type} 失败: {e}", exc_info=True)
            await send_to(ws, {
                "type": "error",
                "message": f"处理失败: {e}",
                "session_id": data.get("session_id"),
            })
    else:
        await send_to(ws, {
            "type": "error",
            "message": f"未知消息类型: {msg_type}",
        })


# ============================================
# 会话管理
# ============================================

async def handle_get_sessions(ws: WebSocket, data: dict):
    sessions = store.list_sessions()
    await send_to(ws, {"type": "session_list", "sessions": sessions})


async def handle_create_session(ws: WebSocket, data: dict):
    sid = uuid.uuid4().hex[:8]
    session = store.create_session(sid)
    await broadcast({"type": "session_created", "session": session})


async def handle_delete_session(ws: WebSocket, data: dict):
    sid = data.get("session_id")
    if not sid:
        return

    # 取消该会话正在进行的处理
    if _engine:
        _engine.cancel(sid)
    task = active_tasks.pop(sid, None)
    if task and not task.done():
        task.cancel()

    store.delete_session(sid)
    await broadcast({"type": "session_deleted", "session_id": sid})


async def handle_get_messages(ws: WebSocket, data: dict):
    sid = data.get("session_id")
    if not sid:
        return

    msgs = store.get_messages(sid)
    frontend_msgs = [_msg_to_segments(m) for m in msgs]

    session = store.get_session(sid)
    pending_options = (session.get("pending_options") or []) if session else []

    resp = {
        "type": "session_messages",
        "session_id": sid,
        "messages": frontend_msgs,
    }
    if pending_options:
        resp["pending_options"] = pending_options
    # 告知前端是否有后台任务仍在运行
    task = active_tasks.get(sid)
    if task and not task.done():
        resp["is_running"] = True
    await send_to(ws, resp)


def _msg_to_segments(msg: dict) -> dict:
    """将存储的消息转换为 segments 格式（兼容新旧两种存储格式）"""
    result = {
        "id": msg.get("id", ""),
        "role": msg.get("role", ""),
    }

    # tokens（来自 metadata）
    meta = msg.get("metadata") or {}
    if meta.get("tokens"):
        result["tokens"] = meta["tokens"]
    if meta.get("prompt_tokens"):
        result["prompt_tokens"] = meta["prompt_tokens"]
    if meta.get("completion_tokens"):
        result["completion_tokens"] = meta["completion_tokens"]

    # 新格式: 已有 segments
    if "segments" in msg:
        result["segments"] = msg["segments"]
        return result

    # 旧格式: content + tool_calls → 转换为 segments
    segments = []

    # 旧格式工具在前
    for tc in msg.get("tool_calls", []):
        segments.append({
            "type": "tool",
            "name": tc.get("name", ""),
            "params": tc.get("params", {}),
            "status": "success" if tc.get("success", True) else "error",
            "result": tc.get("result", ""),
            "duration": tc.get("duration"),
        })

    content = msg.get("content", "")
    if content:
        segments.append({"type": "text", "content": content})

    result["segments"] = segments
    return result


async def handle_update_session_title(ws: WebSocket, data: dict):
    sid = data.get("session_id")
    title = data.get("title", "")
    if not sid:
        return

    store.update_session(sid, title=title)
    await broadcast({
        "type": "session_title_updated",
        "session_id": sid,
        "title": title,
    })


# ============================================
# AI 消息处理
# ============================================

async def handle_send_message(ws: WebSocket, data: dict):
    """处理用户发送的消息 → 启动 AI 引擎"""
    sid = data.get("session_id")
    content = data.get("content", "").strip()

    if not sid or not content:
        return

    # 检查会话是否存在
    session = store.get_session(sid)
    if not session:
        await send_to(ws, {
            "type": "error", "session_id": sid,
            "message": "会话不存在",
        })
        return

    # 初始化引擎
    _init_engine()

    # ★ Fix B2: 取消旧任务再启新任务
    old_task = active_tasks.pop(sid, None)
    if old_task and not old_task.done():
        old_task.cancel()
        if _engine:
            _engine.cancel(sid)
    # 注册 sid 到该 ws（用于 WebSocket 断开时清理）
    if ws in _ws_sessions:
        _ws_sessions[ws].add(sid)

    # 保存用户消息到前端展示 (engine 会另存到 context)
    user_msg_id = f"user_{uuid.uuid4().hex[:8]}"
    turn_id = f"turn_{uuid.uuid4().hex[:8]}"
    store.add_message(sid, "user", msg_id=user_msg_id,
                      segments=[{"type": "text", "content": content}],
                      metadata={"turn_id": turn_id})
    store.update_session(sid, pending_options=None)

    # 启动异步 AI 处理
    task = asyncio.create_task(
        _process_ai_message(ws, sid, content, user_msg_id, turn_id)
    )
    active_tasks[sid] = task


async def handle_cancel(ws: WebSocket, data: dict):
    """取消正在处理的 AI 请求"""
    sid = data.get("session_id")
    if not sid:
        return

    if _engine:
        _engine.cancel(sid)

    task = active_tasks.pop(sid, None)
    if task and not task.done():
        task.cancel()

    await send_to(ws, {"type": "done", "session_id": sid})


async def handle_fork_session(ws: WebSocket, data: dict):
    """从指定消息分叉出新会话"""
    src_id = data.get("session_id")
    anchor_msg_id = data.get("message_id")
    title = data.get("title", "分叉对话")

    if not src_id or not anchor_msg_id:
        await send_to(ws, {
            "type": "error", "session_id": src_id,
            "message": "缺少 session_id 或 message_id",
        })
        return

    cfg = get_config()
    session = store.fork_session(
        src_id, anchor_msg_id, title=title,
        cwd=cfg.engine.working_directory,
        memory_dir=cfg.engine.memory_dir,
    )
    if not session:
        await send_to(ws, {
            "type": "error", "session_id": src_id,
            "message": "分叉失败: 找不到指定消息",
        })
        return

    await broadcast({"type": "session_forked", "session": session})


async def handle_confirm_tools(ws: WebSocket, data: dict):
    """用户确认/取消危险工具执行"""
    session_id = data.get("session_id")
    confirmed_ids = set(data.get("confirmed", []))   # 用户点击运行的 tool_call id
    skipped_ids = set(data.get("skipped", []))       # 用户点击跳过的 tool_call id

    if not session_id:
        return

    pending = _pending_confirms.pop(session_id, None)
    if not pending:
        await send_to(ws, {"type": "error", "session_id": session_id,
                           "message": "没有待确认的工具"})
        return

    pending_tcs = pending["pending_tool_calls"]
    msg_id = pending["msg_id"]
    orig_segments = pending["segments"]
    orig_msg_tokens = pending["msg_tokens"]

    _init_engine()
    cfg = get_config()

    # -- 执行确认的工具 + 写入取消结果 --
    confirmed_names: list = []
    new_segments = list(orig_segments)
    confirm_segments: list = []  # 本次确认产生的 tool segments，待追加到 orig msg
    resume_msg_tokens = orig_msg_tokens

    for tc in pending_tcs:
        tc_id = tc.get("id") or f"call_{tc['function']['name']}_confirm"
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"].get("arguments", "{}") or "{}")
        except json.JSONDecodeError:
            args = {}

        if tc_id in confirmed_ids or (not confirmed_ids and not skipped_ids):
            # 确认执行
            confirmed_names.append(name)
            t0 = time.time()
            await broadcast({"type": "tool_start", "session_id": session_id,
                              "tool_id": tc_id,
                              "tool_name": name, "params": args})
            try:
                result = await _mcp.call(name, args)
                duration_ms = int((time.time() - t0) * 1000)
                success = result.success
                result_str = json.dumps(result.data, ensure_ascii=False) if result.data else (result.error or "")
            except Exception as e:
                duration_ms = int((time.time() - t0) * 1000)
                success = False
                result_str = str(e)
            compact = _compact_result(result_str)
            await broadcast({"type": "tool_end", "session_id": session_id,
                              "tool_id": tc_id,
                              "tool_name": name, "success": success, "duration": duration_ms,
                              "result": compact})
            store.add_context_entry(session_id, {
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": result_str,
                "metadata": {"success": success, "duration_ms": duration_ms},
            })
            seg = {
                "type": "tool", "name": name, "params": args,
                "status": "success" if success else "error",
                "result": compact,
                "duration": duration_ms,
            }
            new_segments.append(seg)
            confirm_segments.append(seg)
        else:
            # 取消：写入 cancelled 结果（保持协议完整性）
            store.add_context_entry(session_id, {
                "role": "tool",
                "tool_call_id": tc_id,
                "name": name,
                "content": "用户已取消此操作",
                "metadata": {"success": False, "cancelled": True},
            })
            confirm_segments.append({
                "type": "tool", "name": name, "params": args,
                "status": "error",
                "result": "用户已取消此操作",
                "duration": 0,
            })

    # 将本次确认的 tool 段追加到原消息（与前端 pending → 终态 渲染对齐）
    if confirm_segments:
        store.append_to_message(session_id, msg_id, confirm_segments)

    # 写入确认工具元数据（用于会话重载时恢复 confirmed_tools）
    if confirmed_names:
        store.add_context_entry(session_id, {
            "role": "system",
            "content": "",
            "metadata": {"confirmed_tools": confirmed_names},
        })

    # -- 继续让模型基于工具结果回复 --
    task = asyncio.create_task(
        _process_ai_message_resume(ws, session_id, msg_id, new_segments, resume_msg_tokens)
    )
    active_tasks[session_id] = task


async def _process_ai_message(ws: WebSocket, session_id: str,
                               content: str, user_msg_id: str,
                               turn_id: str):
    """运行 AI 引擎并推送结果到前端（segments 模型）"""
    msg_id = f"ai_{uuid.uuid4().hex[:8]}"

    # segments: 按时间顺序记录文本和工具调用
    segments = []
    msg_tokens = 0  # 本轮消息累积 total_tokens
    msg_prompt_tokens = 0  # 本轮最后一次 LLM 调用的 prompt_tokens (= 当前上下文大小)
    msg_completion_tokens = 0  # 本轮最后一次 LLM 调用的 completion_tokens
    _msg_committed = False

    def _build_stored_segs():
        out = []
        for s in segments:
            if s["type"] in ("text", "thinking"):
                out.append({"type": s["type"], "content": s["content"]})
            elif s["type"] == "tool":
                out.append({
                    "type": "tool", "name": s["name"], "params": s["params"],
                    "status": s["status"],
                    "result": _compact_result(s["result"]) if s["result"] else "",
                    "duration": s["duration"],
                })
            elif s["type"] == "compress":
                out.append({
                    "type": "compress",
                    "id": s.get("id", ""),
                    "status": s.get("status", "success"),
                    "archived": s.get("archived", 0),
                    "archived_tokens": s.get("archived_tokens", s.get("prompt_tokens", 0)),
                    "prompt_tokens": s.get("archived_tokens", s.get("prompt_tokens", 0)),
                    "title": s.get("title", ""),
                    "file": s.get("file", ""),
                    "error": s.get("error", ""),
                })
        return out

    # 发送 message_start
    await send_to(ws, {
        "type": "message_start",
        "session_id": session_id,
        "message_id": msg_id,
    })

    try:
        # ---- 回调: 引擎事件 → segments 追踪 + WebSocket 推送 ----

        async def on_thinking(text: str):
            """thinking 流式文本 → 追加到 thinking segment"""
            if segments and segments[-1]["type"] == "thinking":
                segments[-1]["content"] += text
            else:
                segments.append({"type": "thinking", "content": text})

            await send_to(ws, {
                "type": "thinking_delta",
                "session_id": session_id,
                "content": text,
            })

        async def on_text(text: str):
            """流式文本 → 追加到最后一个 text segment"""
            if segments and segments[-1]["type"] == "text":
                segments[-1]["content"] += text
            else:
                segments.append({"type": "text", "content": text})

            await send_to(ws, {
                "type": "message_delta",
                "session_id": session_id,
                "content": text,
            })

        async def on_tool_start(name: str, params: dict):
            """工具开始 → 创建新 tool segment"""
            tool_id = f"tool_{uuid.uuid4().hex[:6]}"
            segments.append({
                "type": "tool",
                "id": tool_id,
                "name": name,
                "params": params,
                "status": "running",
                "result": None,
                "duration": None,
            })
            await send_to(ws, {
                "type": "tool_start",
                "session_id": session_id,
                "tool_name": name,
                "tool_id": tool_id,
                "params": params,
            })

        async def on_tool_end(name: str, result: dict, success: bool,
                              duration: int):
            """工具结束 → 更新对应的 tool segment"""
            result_display = ""
            if success and result.get("data"):
                result_display = result["data"]
            elif not success and result.get("error"):
                result_display = result["error"]

            result_display = _compact_result(result_display)

            # 找到匹配的 tool segment（最后一个 running 且同名的）
            tool_id = None
            for seg in reversed(segments):
                if (seg["type"] == "tool"
                        and seg["name"] == name
                        and seg["status"] == "running"):
                    seg["status"] = "success" if success else "error"
                    seg["result"] = result_display
                    seg["duration"] = duration
                    tool_id = seg.get("id")
                    break

            await send_to(ws, {
                "type": "tool_end",
                "session_id": session_id,
                "tool_name": name,
                "tool_id": tool_id,
                "success": success,
                "result": result_display,
                "duration": duration,
            })

        async def on_model_info(model: str):
            await send_to(ws, {
                "type": "model_info",
                "session_id": session_id,
                "model": model,
            })

        async def on_title_update(title: str):
            store.update_session(session_id, title=title)
            await broadcast({
                "type": "session_title_updated",
                "session_id": session_id,
                "title": title,
            })

        async def on_usage(prompt: int, completion: int, total: int):
            """LLM 调用完成后回调用量。total 累加; prompt/completion 用最新一次。"""
            nonlocal msg_tokens, msg_prompt_tokens, msg_completion_tokens
            msg_tokens += total
            if prompt > 0:
                msg_prompt_tokens = prompt
                msg_completion_tokens = completion

        async def on_compress_start(archived: int, archived_tokens: int):
            """上下文压缩开始: 类似 tool_start, 推一段 compress segment。"""
            seg_id = f"cmp_{uuid.uuid4().hex[:6]}"
            segments.append({
                "type": "compress",
                "id": seg_id,
                "status": "running",
                "archived": archived,
                "archived_tokens": archived_tokens,
                "prompt_tokens": archived_tokens,
                "title": "",
                "file": "",
                "error": "",
            })
            await send_to(ws, {
                "type": "compress_start",
                "session_id": session_id,
                "seg_id": seg_id,
                "archived": archived,
                "archived_tokens": archived_tokens,
                "prompt_tokens": archived_tokens,
            })

        async def on_compress_end(success: bool, info: dict):
            """上下文压缩结束: 更新最后一个 running 的 compress segment。"""
            for seg in reversed(segments):
                if seg.get("type") == "compress" and seg.get("status") == "running":
                    seg["status"] = "success" if success else "error"
                    seg["title"] = info.get("title", "")
                    seg["file"] = info.get("file", "")
                    seg["archived_tokens"] = info.get("archived_tokens", seg.get("archived_tokens", 0))
                    seg["error"] = info.get("error", "")
                    break
            await send_to(ws, {
                "type": "compress_end",
                "session_id": session_id,
                "success": success,
                "title": info.get("title", ""),
                "file": info.get("file", ""),
                "archived_tokens": info.get("archived_tokens", 0),
                "error": info.get("error", ""),
            })

        callbacks = EngineCallbacks(
            on_text=on_text,
            on_thinking=on_thinking,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_model_info=on_model_info,
            on_title_update=on_title_update,
            on_usage=on_usage,
            on_compress_start=on_compress_start,
            on_compress_end=on_compress_end,
        )

        # 运行引擎
        cfg = get_config()
        result = await _engine.run(
            session_id, content, callbacks,
            auto_confirm_dangerous=cfg.engine.auto_confirm_dangerous,
            user_msg_id=user_msg_id,
            ai_msg_id=msg_id,
            turn_id=turn_id,
        )

        # Token 统计：一次性写入
        if msg_tokens > 0 or msg_prompt_tokens > 0:
            store.update_session_tokens(session_id, msg_tokens,
                                         last_prompt_tokens=msg_prompt_tokens or None,
                                         last_completion_tokens=msg_completion_tokens,
                                         last_msg_tokens=msg_tokens or None)
            await broadcast({"type": "tokens_update", "session_id": session_id})

        # 存储: 截断 tool result 后保存 segments
        stored_segments = []
        for seg in segments:
            if seg["type"] == "text":
                stored_segments.append({
                    "type": "text",
                    "content": seg["content"],
                })
            elif seg["type"] == "thinking":
                stored_segments.append({
                    "type": "thinking",
                    "content": seg["content"],
                })
            elif seg["type"] == "tool":
                stored_segments.append({
                    "type": "tool",
                    "name": seg["name"],
                    "params": seg["params"],
                    "status": seg["status"],
                    "result": _compact_result(seg["result"]) if seg["result"] else "",
                    "duration": seg["duration"],
                })
            elif seg["type"] == "compress":
                stored_segments.append({
                    "type": "compress",
                    "id": seg.get("id", ""),
                    "status": seg.get("status", "success"),
                    "archived": seg.get("archived", 0),
                    "archived_tokens": seg.get("archived_tokens", seg.get("prompt_tokens", 0)),
                    "prompt_tokens": seg.get("archived_tokens", seg.get("prompt_tokens", 0)),
                    "title": seg.get("title", ""),
                    "file": seg.get("file", ""),
                    "error": seg.get("error", ""),
                })

        store.add_message(
            session_id, "assistant",
            msg_id=msg_id,
            segments=stored_segments,
            metadata={"tokens": msg_tokens,
                      "prompt_tokens": msg_prompt_tokens,
                      "completion_tokens": msg_completion_tokens,
                      "turn_id": turn_id},
        )
        _msg_committed = True

        # message_end: 广播（后台运行时支持重新连接的客户端）
        final_text = result.text if result else ""
        _sess_after = store.get_session(session_id) or {}
        await broadcast({
            "type": "message_end",
            "session_id": session_id,
            "message_id": msg_id,
            "content": final_text,
            "tokens": msg_tokens,
            "prompt_tokens": msg_prompt_tokens,
            "completion_tokens": msg_completion_tokens,
            "session_total_tokens": _sess_after.get("tokens", 0),
        })

        # 最终状态
        if result.is_pending_confirm:
            # 危险工具等待确认：保存状态，发送 pending_confirm 事件
            _pending_confirms[session_id] = {
                "ws": ws,
                "msg_id": msg_id,
                "pending_tool_calls": result.pending_tool_calls,
                "segments": list(segments),
                "msg_tokens": msg_tokens,
            }
            tools_info = []
            for tc in result.pending_tool_calls:
                try:
                    args = json.loads(tc["function"].get("arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tools_info.append({
                    "id": tc.get("id") or f"call_{tc['function']['name']}",
                    "name": tc["function"]["name"],
                    "args": args,
                })
            await send_to(ws, {
                "type": "pending_confirm",
                "session_id": session_id,
                "message_id": msg_id,
                "tools": tools_info,
            })
        elif result.is_ask:
            opts = result.options or []
            store.update_session(session_id, pending_options=opts)
            evt = {
                "type": "ask",
                "session_id": session_id,
                "question": result.text,
            }
            if opts:
                evt["options"] = opts
            await send_to(ws, evt)
        elif result.is_error:
            await send_to(ws, {
                "type": "error",
                "session_id": session_id,
                "message": result.text,
            })
        else:
            await broadcast({
                "type": "done",
                "session_id": session_id,
            })

    except asyncio.CancelledError:
        pass  # 由 finally 保存并广播

    except LLMError as e:
        logger.error(f"LLM 错误: {e}")
        segments.append({"type": "text", "content": f"[AI 服务错误: {e}]"})
        await broadcast({"type": "error", "session_id": session_id,
                         "message": f"AI 服务错误: {e}"})

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)
        segments.append({"type": "text", "content": "[服务器内部错误，请查看日志]"})
        await broadcast({"type": "error", "session_id": session_id,
                         "message": "服务器内部错误，请查看日志"})

    finally:
        # 中断/异常时保存已生成的部分消息
        if not _msg_committed:
            stored = _build_stored_segs()
            if stored:
                store.add_message(session_id, "assistant", msg_id=msg_id,
                                  segments=stored,
                                  metadata={"tokens": msg_tokens,
                                            "prompt_tokens": msg_prompt_tokens,
                                            "completion_tokens": msg_completion_tokens,
                                            "turn_id": turn_id,
                                            "interrupted": True})
                if msg_tokens > 0 or msg_prompt_tokens > 0:
                    store.update_session_tokens(session_id, msg_tokens,
                                                 last_prompt_tokens=msg_prompt_tokens or None,
                                                 last_completion_tokens=msg_completion_tokens,
                                                 last_msg_tokens=msg_tokens or None)
                    await broadcast({"type": "tokens_update", "session_id": session_id})
                last_text = next(
                    (s["content"] for s in reversed(stored) if s["type"] == "text"), ""
                )
                _sess_after = store.get_session(session_id) or {}
                await broadcast({"type": "message_end", "session_id": session_id,
                                  "message_id": msg_id, "content": last_text,
                                  "tokens": msg_tokens,
                                  "prompt_tokens": msg_prompt_tokens,
                                  "completion_tokens": msg_completion_tokens,
                                  "session_total_tokens": _sess_after.get("tokens", 0)})
                await broadcast({"type": "done", "session_id": session_id})
        # 只移除当前 task（避免误删后续新 task）
        current_task = asyncio.current_task()
        if active_tasks.get(session_id) is current_task:
            active_tasks.pop(session_id, None)


async def _process_ai_message_resume(
        ws: WebSocket, session_id: str,
        orig_msg_id: str, orig_segments: list, orig_tokens: int,
):
    """确认危险工具后，继续基于当前上下文获取模型回复。

    此时 store context 已包含：
      [user msg] [assistant: tool_calls] [tool: results (confirmed/cancelled)]
    调用 engine.run(session_id, None, ...) 跳过写 user 消息，直接续跑模型循环。

    复用 orig_msg_id：前端把后续内容追加到同一个消息气泡，
    存储侧用 append_to_message 把新 segments 合并进原消息。
    """
    segments: list = []
    msg_tokens = 0
    msg_prompt_tokens = 0
    msg_completion_tokens = 0
    _msg_committed = False

    def _build_stored_segs():
        out = []
        for s in segments:
            if s["type"] in ("text", "thinking"):
                out.append({"type": s["type"], "content": s["content"]})
            elif s["type"] == "tool":
                out.append({
                    "type": "tool", "name": s["name"], "params": s["params"],
                    "status": s["status"],
                    "result": _compact_result(s["result"]) if s["result"] else "",
                    "duration": s["duration"],
                })
            elif s["type"] == "compress":
                out.append({
                    "type": "compress",
                    "id": s.get("id", ""),
                    "status": s.get("status", "success"),
                    "archived": s.get("archived", 0),
                    "archived_tokens": s.get("archived_tokens", s.get("prompt_tokens", 0)),
                    "prompt_tokens": s.get("archived_tokens", s.get("prompt_tokens", 0)),
                    "title": s.get("title", ""),
                    "file": s.get("file", ""),
                    "error": s.get("error", ""),
                })
        return out

    # 通知前端：继续向原消息气泡追加内容（resume=True 不新建气泡）
    await send_to(ws, {
        "type": "message_start",
        "session_id": session_id,
        "message_id": orig_msg_id,
        "resume": True,
    })

    try:
        async def on_thinking(text: str):
            if segments and segments[-1]["type"] == "thinking":
                segments[-1]["content"] += text
            else:
                segments.append({"type": "thinking", "content": text})
            await send_to(ws, {"type": "thinking_delta",
                                "session_id": session_id, "content": text})

        async def on_text(text: str):
            if segments and segments[-1]["type"] == "text":
                segments[-1]["content"] += text
            else:
                segments.append({"type": "text", "content": text})
            await send_to(ws, {"type": "message_delta",
                                "session_id": session_id, "content": text})

        async def on_tool_start(name: str, params: dict):
            tool_id = f"tool_{uuid.uuid4().hex[:6]}"
            segments.append({"type": "tool", "id": tool_id, "name": name,
                              "params": params, "status": "running",
                              "result": None, "duration": None})
            await send_to(ws, {"type": "tool_start", "session_id": session_id,
                                "tool_name": name, "tool_id": tool_id, "params": params})

        async def on_tool_end(name: str, result: dict, success: bool, duration: int):
            result_display = ""
            if success and result.get("data"):
                result_display = result["data"]
            elif not success and result.get("error"):
                result_display = result["error"]
            result_display = _compact_result(result_display)
            for seg in reversed(segments):
                if seg["type"] == "tool" and seg["name"] == name and seg["status"] == "running":
                    seg["status"] = "success" if success else "error"
                    seg["result"] = result_display
                    seg["duration"] = duration
                    break
            await send_to(ws, {"type": "tool_end", "session_id": session_id,
                                "tool_name": name, "success": success,
                                "result": result_display, "duration": duration})

        async def on_model_info(model: str):
            await send_to(ws, {"type": "model_info",
                                "session_id": session_id, "model": model})

        async def on_title_update(title: str):
            store.update_session(session_id, title=title)
            await broadcast({"type": "session_title_updated",
                              "session_id": session_id, "title": title})

        async def on_usage(prompt: int, completion: int, total: int):
            nonlocal msg_tokens, msg_prompt_tokens, msg_completion_tokens
            msg_tokens += total
            if prompt > 0:
                msg_prompt_tokens = prompt
                msg_completion_tokens = completion

        async def on_compress_start(archived: int, archived_tokens: int):
            seg_id = f"cmp_{uuid.uuid4().hex[:6]}"
            segments.append({
                "type": "compress", "id": seg_id, "status": "running",
                "archived": archived, "archived_tokens": archived_tokens,
                "prompt_tokens": archived_tokens,
                "title": "", "file": "", "error": "",
            })
            await send_to(ws, {
                "type": "compress_start", "session_id": session_id,
                "seg_id": seg_id, "archived": archived,
                "archived_tokens": archived_tokens,
                "prompt_tokens": archived_tokens,
            })

        async def on_compress_end(success: bool, info: dict):
            for seg in reversed(segments):
                if seg.get("type") == "compress" and seg.get("status") == "running":
                    seg["status"] = "success" if success else "error"
                    seg["title"] = info.get("title", "")
                    seg["file"] = info.get("file", "")
                    seg["archived_tokens"] = info.get("archived_tokens", seg.get("archived_tokens", 0))
                    seg["error"] = info.get("error", "")
                    break
            await send_to(ws, {
                "type": "compress_end", "session_id": session_id,
                "success": success,
                "title": info.get("title", ""), "file": info.get("file", ""),
                "archived_tokens": info.get("archived_tokens", 0),
                "error": info.get("error", ""),
            })

        callbacks = EngineCallbacks(
            on_text=on_text, on_thinking=on_thinking,
            on_tool_start=on_tool_start, on_tool_end=on_tool_end,
            on_model_info=on_model_info, on_title_update=on_title_update,
            on_usage=on_usage,
            on_compress_start=on_compress_start,
            on_compress_end=on_compress_end,
        )

        orig_turn_id = ""
        for msg in store.get_messages(session_id):
            if msg.get("id") == orig_msg_id:
                orig_turn_id = (msg.get("metadata") or {}).get("turn_id", "")
                break

        cfg = get_config()
        # user_content=None: 跳过写入新 user 消息，直接从当前上下文续跑
        result = await _engine.run(
            session_id, None, callbacks,
            auto_confirm_dangerous=cfg.engine.auto_confirm_dangerous,
            ai_msg_id=orig_msg_id,
            turn_id=orig_turn_id or None,
        )

        if msg_tokens > 0 or msg_prompt_tokens > 0:
            store.update_session_tokens(session_id, msg_tokens,
                                         last_prompt_tokens=msg_prompt_tokens or None,
                                         last_completion_tokens=msg_completion_tokens,
                                         last_msg_tokens=msg_tokens or None)
            await broadcast({"type": "tokens_update", "session_id": session_id})

        stored_segments = _build_stored_segs()
        store.append_to_message(session_id, orig_msg_id,
                                stored_segments,
                                add_tokens=msg_tokens)
        _msg_committed = True

        final_text = result.text if result else ""
        _sess_after = store.get_session(session_id) or {}
        await broadcast({
            "type": "message_end",
            "session_id": session_id,
            "message_id": orig_msg_id,
            "content": final_text,
            "tokens": msg_tokens,
            "prompt_tokens": msg_prompt_tokens,
            "completion_tokens": msg_completion_tokens,
            "session_total_tokens": _sess_after.get("tokens", 0),
        })

        if result.is_pending_confirm:
            # 再次遇到危险工具（嵌套 invoke 场景）：继续挂在同一个气泡
            _pending_confirms[session_id] = {
                "ws": ws, "msg_id": orig_msg_id,
                "pending_tool_calls": result.pending_tool_calls,
                "segments": list(segments),
                "msg_tokens": orig_tokens + msg_tokens,
            }
            tools_info = []
            for tc in result.pending_tool_calls:
                try:
                    args = json.loads(tc["function"].get("arguments", "{}") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tools_info.append({"id": tc.get("id") or f"call_{tc['function']['name']}",
                                    "name": tc["function"]["name"], "args": args})
            await send_to(ws, {"type": "pending_confirm", "session_id": session_id,
                                "message_id": orig_msg_id, "tools": tools_info})
        elif result.is_ask:
            opts = result.options or []
            store.update_session(session_id, pending_options=opts)
            evt = {"type": "ask", "session_id": session_id, "question": result.text}
            if opts:
                evt["options"] = opts
            await send_to(ws, evt)
        elif result.is_error:
            await send_to(ws, {"type": "error", "session_id": session_id,
                                "message": result.text})
        else:
            await broadcast({"type": "done", "session_id": session_id})

    except asyncio.CancelledError:
        pass  # 由 finally 保存并广播

    except LLMError as e:
        segments.append({"type": "text", "content": f"[AI 服务错误: {e}]"})
        await broadcast({"type": "error", "session_id": session_id,
                         "message": f"AI 服务错误: {e}"})

    except Exception as e:
        logger.error(f"resume 处理异常: {e}", exc_info=True)
        segments.append({"type": "text", "content": "[服务器内部错误]"})
        await broadcast({"type": "error", "session_id": session_id,
                         "message": "服务器内部错误"})

    finally:
        if not _msg_committed:
            stored = _build_stored_segs()
            if stored:
                store.append_to_message(session_id, orig_msg_id, stored,
                                        add_tokens=msg_tokens)
                if msg_tokens > 0 or msg_prompt_tokens > 0:
                    store.update_session_tokens(session_id, msg_tokens,
                                                 last_prompt_tokens=msg_prompt_tokens or None,
                                                 last_completion_tokens=msg_completion_tokens,
                                                 last_msg_tokens=msg_tokens or None)
                    await broadcast({"type": "tokens_update", "session_id": session_id})
                last_text = next(
                    (s["content"] for s in reversed(stored) if s["type"] == "text"), ""
                )
                _sess_after = store.get_session(session_id) or {}
                await broadcast({"type": "message_end", "session_id": session_id,
                                  "message_id": orig_msg_id, "content": last_text,
                                  "tokens": msg_tokens,
                                  "prompt_tokens": msg_prompt_tokens,
                                  "completion_tokens": msg_completion_tokens,
                                  "session_total_tokens": _sess_after.get("tokens", 0)})
                await broadcast({"type": "done", "session_id": session_id})
        current_task = asyncio.current_task()
        if active_tasks.get(session_id) is current_task:
            active_tasks.pop(session_id, None)

async def handle_get_config(ws: WebSocket, data: dict):
    """获取当前配置 (API Key 遮蔽)"""
    cfg = get_config()
    await send_to(ws, {
        "type": "config_data",
        "config": cfg.to_dict(mask_key=True),
    })


async def handle_save_config(ws: WebSocket, data: dict):
    """保存配置"""
    new_config = data.get("config", {})
    if not new_config:
        await send_to(ws, {"type": "error", "message": "配置数据为空"})
        return

    try:
        cfg = get_config()
        cfg.update_from_dict(new_config)
        cfg.save()

        # 运行时更新组件
        await _reinit_components()

        await send_to(ws, {
            "type": "config_saved",
            "config": cfg.to_dict(mask_key=True),
            "message": "配置已保存",
        })
    except Exception as e:
        logger.error(f"保存配置失败: {e}", exc_info=True)
        await send_to(ws, {
            "type": "error",
            "message": f"保存失败: {e}",
        })


async def handle_test_llm(ws: WebSocket, data: dict):
    """测试 LLM 连接"""
    _init_engine()
    cfg = get_config()

    if not cfg.llm.api_key:
        await send_to(ws, {
            "type": "test_result",
            "target": "llm",
            "success": False,
            "message": "API Key 未配置",
        })
        return

    try:
        response = await _llm.chat([
            {"role": "user", "content": "回复OK"}
        ])
        await send_to(ws, {
            "type": "test_result",
            "target": "llm",
            "success": True,
            "message": f"模型 {response.model} 连接正常",
        })
    except Exception as e:
        await send_to(ws, {
            "type": "test_result",
            "target": "llm",
            "success": False,
            "message": str(e),
        })


async def handle_test_axon(ws: WebSocket, data: dict):
    """测试 Axon MCP Server 连接"""
    _init_engine()

    try:
        if not _mcp.connected:
            connected = await _mcp.connect()
            if not connected:
                raise Exception(
                    f"无法连接到 {_mcp.host}:{_mcp.port}"
                )

        ok = await _mcp.ping()
        if ok:
            await send_to(ws, {
                "type": "test_result",
                "target": "axon",
                "success": True,
                "message": f"Axon ({_mcp.host}:{_mcp.port}) 连接正常",
            })
        else:
            await send_to(ws, {
                "type": "test_result",
                "target": "axon",
                "success": False,
                "message": "Ping 失败",
            })
    except Exception as e:
        await send_to(ws, {
            "type": "test_result",
            "target": "axon",
            "success": False,
            "message": str(e),
        })


async def handle_restart_axon(ws: WebSocket, data: dict):
    """重启 Axon 子进程"""
    axon_mgr = _get_axon_manager()
    if not axon_mgr:
        await send_to(ws, {
            "type": "test_result",
            "target": "axon",
            "success": False,
            "message": "Axon 未由 Orion 管理",
        })
        return

    if axon_mgr.is_external:
        await send_to(ws, {
            "type": "test_result",
            "target": "axon",
            "success": False,
            "message": "Axon 由外部进程管理，无法重启",
        })
        return

    try:
        # 断开当前 MCP 连接
        if _mcp and _mcp.connected:
            await _mcp.disconnect()

        ok = await axon_mgr.restart()
        if ok:
            await send_to(ws, {
                "type": "test_result",
                "target": "axon",
                "success": True,
                "message": "Axon 重启成功",
            })
        else:
            await send_to(ws, {
                "type": "test_result",
                "target": "axon",
                "success": False,
                "message": "Axon 重启失败",
            })
    except Exception as e:
        await send_to(ws, {
            "type": "test_result",
            "target": "axon",
            "success": False,
            "message": f"重启出错: {e}",
        })


# ============================================
# 消息处理器映射
# ============================================

async def handle_list_files(ws: WebSocket, data: dict):
    """列出指定目录下的文件和子目录"""
    _init_engine()

    path = data.get("path", "")
    if not path:
        cfg = get_config()
        path = cfg.get_working_directory()

    try:
        if not _mcp.connected:
            connected = await _mcp.connect()
            if not connected:
                await send_to(ws, {
                    "type": "file_list",
                    "path": path,
                    "entries": [],
                    "error": "Axon 未连接",
                })
                return

        result = await _mcp.call("list_directory", {"path": path, "include_hidden": True})
        if result.success:
            entries = result.data.get("entries", [])
            await send_to(ws, {
                "type": "file_list",
                "path": path,
                "entries": entries,
            })
        else:
            await send_to(ws, {
                "type": "file_list",
                "path": path,
                "entries": [],
                "error": result.error or "列目录失败",
            })
    except Exception as e:
        await send_to(ws, {
            "type": "file_list",
            "path": path,
            "entries": [],
            "error": str(e),
        })


async def handle_read_file_content(ws: WebSocket, data: dict):
    """读取文件内容"""
    _init_engine()

    path = data.get("path", "")
    if not path:
        await send_to(ws, {
            "type": "file_content",
            "path": "",
            "error": "未指定文件路径",
        })
        return

    try:
        if not _mcp.connected:
            connected = await _mcp.connect()
            if not connected:
                await send_to(ws, {
                    "type": "file_content",
                    "path": path,
                    "error": "Axon 未连接",
                })
                return

        result = await _mcp.call("read_file", {
            "path": path,
            "max_size": 512 * 1024,  # 512KB 上限
        })
        if result.success:
            await send_to(ws, {
                "type": "file_content",
                "path": path,
                "content": result.data.get("content", ""),
                "encoding": result.data.get("encoding", "utf-8"),
                "size": result.data.get("size", 0),
            })
        else:
            await send_to(ws, {
                "type": "file_content",
                "path": path,
                "error": result.error or "读取失败",
            })
    except Exception as e:
        await send_to(ws, {
            "type": "file_content",
            "path": path,
            "error": str(e),
        })


async def handle_save_file_content(ws: WebSocket, data: dict):
    """保存文件内容"""
    _init_engine()

    path = data.get("path", "")
    content = data.get("content", "")

    if not path:
        await send_to(ws, {
            "type": "file_saved",
            "path": "",
            "success": False,
            "error": "未指定文件路径",
        })
        return

    try:
        if not _mcp.connected:
            connected = await _mcp.connect()
            if not connected:
                await send_to(ws, {
                    "type": "file_saved",
                    "path": path,
                    "success": False,
                    "error": "Axon 未连接",
                })
                return

        result = await _mcp.call("write_file", {
            "path": path,
            "content": content,
        })
        if result.success:
            await send_to(ws, {
                "type": "file_saved",
                "path": path,
                "success": True,
            })
        else:
            await send_to(ws, {
                "type": "file_saved",
                "path": path,
                "success": False,
                "error": result.error or "保存失败",
            })
    except Exception as e:
        await send_to(ws, {
            "type": "file_saved",
            "path": path,
            "success": False,
            "error": str(e),
        })


MESSAGE_HANDLERS = {
    # 会话
    "get_sessions": handle_get_sessions,
    "create_session": handle_create_session,
    "delete_session": handle_delete_session,
    "fork_session": handle_fork_session,
    "get_messages": handle_get_messages,
    "send_message": handle_send_message,
    "update_session_title": handle_update_session_title,
    "cancel": handle_cancel,
    "confirm_tools": handle_confirm_tools,   # 危险工具确认
    # 设置
    "get_config": handle_get_config,
    "save_config": handle_save_config,
    "test_llm": handle_test_llm,
    "test_axon": handle_test_axon,
    "restart_axon": handle_restart_axon,
    # 文件浏览
    "list_files": handle_list_files,
    "read_file_content": handle_read_file_content,
    "save_file_content": handle_save_file_content,
}


# ============================================
# 静态文件 & 开发热刷新
# ============================================

@app.get("/__dev_mtime")
async def dev_mtime():
    """返回 web 目录所有文件中最新的修改时间戳"""
    latest = 0.0
    for f in WEB_DIR.rglob("*"):
        if f.is_file():
            mt = f.stat().st_mtime
            if mt > latest:
                latest = mt
    return {"mtime": latest}

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
