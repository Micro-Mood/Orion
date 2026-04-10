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
import json
import logging
import uuid
from pathlib import Path
from typing import Dict, List

import builtins

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import get_config
from engine import EngineCallbacks, OrionEngine
from llm import LLMClient, LLMError
from mcp_client import MCPClient
from store import SessionStore

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Orion")


# ============================================
# 全局状态
# ============================================
store = SessionStore()
connections: List[WebSocket] = []
# 每个会话当前正在处理的 task
active_tasks: Dict[str, asyncio.Task] = {}

# 延迟初始化
_engine: OrionEngine = None
_mcp: MCPClient = None
_llm: LLMClient = None

# 文件系统监控
_fs_observer: Observer = None
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
    global _fs_observer
    if _fs_observer:
        _fs_observer.stop()
        _fs_observer = None


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
        max_history=cfg.engine.max_history,
        max_iterations=cfg.engine.max_iterations,
        working_directory=cfg.get_working_directory(),
    )

    # 启动文件系统监控
    cwd = cfg.get_working_directory()
    if cwd and Path(cwd).is_dir():
        _start_fs_watcher(cwd)


def _get_axon_manager():
    """获取 main.py 传入的 AxonManager 实例"""
    return getattr(builtins, "_orion_axon_mgr", None)


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
        _engine.max_history = cfg.engine.max_history
        _engine.max_iterations = cfg.engine.max_iterations
        _engine.cwd = cfg.get_working_directory()

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
        pass


async def broadcast(data: dict, exclude: WebSocket = None):
    """广播消息到所有连接"""
    for ws in connections:
        if ws is not exclude:
            await send_to(ws, data)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connections.append(ws)
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
    await send_to(ws, resp)


def _msg_to_segments(msg: dict) -> dict:
    """将存储的消息转换为 segments 格式（兼容新旧两种存储格式）"""
    result = {
        "id": msg.get("id", ""),
        "role": msg.get("role", ""),
    }

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

    # 保存用户消息到前端展示 (engine 会另存到 context)
    user_msg_id = f"user_{uuid.uuid4().hex[:8]}"
    store.add_message(sid, "user", msg_id=user_msg_id,
                      segments=[{"type": "text", "content": content}])
    store.update_session(sid, pending_options=None)

    # 启动异步 AI 处理
    task = asyncio.create_task(_process_ai_message(ws, sid, content))
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


async def _process_ai_message(ws: WebSocket, session_id: str,
                               content: str):
    """运行 AI 引擎并推送结果到前端（segments 模型）"""
    msg_id = f"ai_{uuid.uuid4().hex[:8]}"

    # segments: 按时间顺序记录文本和工具调用
    segments = []

    # 发送 message_start
    await send_to(ws, {
        "type": "message_start",
        "session_id": session_id,
        "message_id": msg_id,
    })

    try:
        # ---- 回调: 引擎事件 → segments 追踪 + WebSocket 推送 ----

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

        callbacks = EngineCallbacks(
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            on_model_info=on_model_info,
        )

        # 运行引擎
        result = await _engine.run(session_id, content, callbacks)

        # 存储: 截断 tool result 后保存 segments
        stored_segments = []
        for seg in segments:
            if seg["type"] == "text":
                stored_segments.append({
                    "type": "text",
                    "content": seg["content"],
                })
            elif seg["type"] == "tool":
                stored_segments.append({
                    "type": "tool",
                    "name": seg["name"],
                    "params": seg["params"],
                    "status": seg["status"],
                    "result": (seg["result"][:500]
                               if seg["result"] else ""),
                    "duration": seg["duration"],
                })

        store.add_message(
            session_id, "assistant",
            msg_id=msg_id,
            segments=stored_segments,
        )

        # message_end: 发送最终文本内容
        final_text = result.text if result else ""
        await send_to(ws, {
            "type": "message_end",
            "session_id": session_id,
            "message_id": msg_id,
            "content": final_text,
        })

        # 最终状态
        if result.is_ask:
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
            await send_to(ws, {
                "type": "done",
                "session_id": session_id,
            })

    except asyncio.CancelledError:
        await send_to(ws, {
            "type": "message_end",
            "session_id": session_id,
            "message_id": msg_id,
            "content": "已取消",
        })
        await send_to(ws, {"type": "done", "session_id": session_id})

    except LLMError as e:
        logger.error(f"LLM 错误: {e}")
        await send_to(ws, {
            "type": "message_end",
            "session_id": session_id,
            "message_id": msg_id,
            "content": "",
        })
        await send_to(ws, {
            "type": "error",
            "session_id": session_id,
            "message": f"AI 服务错误: {e}",
        })

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)
        await send_to(ws, {
            "type": "message_end",
            "session_id": session_id,
            "message_id": msg_id,
            "content": "",
        })
        await send_to(ws, {
            "type": "error",
            "session_id": session_id,
            "message": f"服务器内部错误: {e}",
        })

    finally:
        active_tasks.pop(session_id, None)


# ============================================
# 设置管理
# ============================================

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

        result = await _mcp.call("list_directory", {"path": path})
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


MESSAGE_HANDLERS = {
    # 会话
    "get_sessions": handle_get_sessions,
    "create_session": handle_create_session,
    "delete_session": handle_delete_session,
    "get_messages": handle_get_messages,
    "send_message": handle_send_message,
    "update_session_title": handle_update_session_title,
    "cancel": handle_cancel,
    # 设置
    "get_config": handle_get_config,
    "save_config": handle_save_config,
    "test_llm": handle_test_llm,
    "test_axon": handle_test_axon,
    "restart_axon": handle_restart_axon,
    # 文件浏览
    "list_files": handle_list_files,
    "read_file_content": handle_read_file_content,
}


# ============================================
# 生命周期
# ============================================

@app.on_event("shutdown")
async def shutdown():
    """清理资源"""
    _stop_fs_watcher()
    if _llm:
        await _llm.close()
    if _mcp:
        await _mcp.disconnect()


# ============================================
# 静态文件 & 开发热刷新
# ============================================

@app.get("/__dev_mtime")
async def dev_mtime():
    """返回 web 目录所有文件中最新的修改时间戳"""
    latest = 0.0
    for f in WEB_DIR.iterdir():
        if f.is_file():
            mt = f.stat().st_mtime
            if mt > latest:
                latest = mt
    return {"mtime": latest}

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
