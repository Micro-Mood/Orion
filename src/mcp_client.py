"""
Orion MCP Client
================

异步 TCP 客户端，通过 JSON-RPC 2.0 连接 Axon MCP Server。
行分隔协议（每个 JSON 消息以 \\n 结尾）。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPResult:
    """MCP 调用结果"""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class MCPClient:
    """
    Axon MCP Server 异步 TCP 客户端
    
    协议: JSON-RPC 2.0，行分隔（\\n）
    请求: {"jsonrpc": "2.0", "method": "xxx", "params": {...}, "id": N}
    响应: {"jsonrpc": "2.0", "result": {...}, "id": N}
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9100,
                 connect_timeout: float = 5.0, default_timeout: float = 60.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.default_timeout = default_timeout
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._request_id = 0
        self._connected = False
        # 单连接串行收发，避免并发调用时响应错配
        self._io_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None

    async def connect(self) -> bool:
        """连接 Axon MCP Server"""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.connect_timeout
            )
            self._connected = True
            logger.info(f"MCP 已连接: {self.host}:{self.port}")
            return True
        except asyncio.TimeoutError:
            logger.error(f"MCP 连接超时: {self.host}:{self.port}")
            self._connected = False
            return False
        except OSError as e:
            logger.error(f"MCP 连接失败: {self.host}:{self.port} - {e}")
            self._connected = False
            return False

    async def disconnect(self):
        """断开连接"""
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._connected = False
        logger.info("MCP 已断开")

    async def call(self, method: str, params: Optional[Dict[str, Any]] = None,
                   timeout: Optional[float] = None) -> MCPResult:
        """
        调用 MCP 方法
        
        Args:
            method: 方法名
            params: 参数字典
            timeout: 超时秒数（None 则根据方法自动推断）
            
        Returns:
            MCPResult 结果
        """
        if not self.connected:
            return MCPResult(success=False, error="MCP 未连接")

        # 超时推断
        if timeout is None:
            timeout = self._infer_timeout(method, params)

        try:
            async with self._io_lock:
                # 构建请求
                self._request_id += 1
                request_id = self._request_id
                request = {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params or {},
                    "id": request_id,
                }

                # 发送请求
                request_line = json.dumps(request, ensure_ascii=False) + "\n"
                self._writer.write(request_line.encode("utf-8"))
                await self._writer.drain()

                # 读取并匹配响应（忽略超时后残留的旧响应）
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()

                    response_line = await asyncio.wait_for(
                        self._reader.readline(),
                        timeout=remaining,
                    )

                    if not response_line:
                        self._connected = False
                        return MCPResult(success=False, error="MCP 连接已关闭")

                    try:
                        response = json.loads(response_line.decode("utf-8"))
                    except json.JSONDecodeError:
                        logger.warning("MCP 收到非法 JSON 响应，已忽略")
                        continue

                    # 仅接收当前请求的响应，其他响应视为陈旧包并忽略
                    if response.get("id") != request_id:
                        logger.warning(
                            "MCP 响应 ID 不匹配: expect=%s, got=%s, method=%s",
                            request_id,
                            response.get("id"),
                            method,
                        )
                        continue

                    return self._parse_response(response)

        except asyncio.TimeoutError:
            logger.warning(f"MCP 调用超时: {method} ({timeout}s)")
            return MCPResult(success=False, error=f"调用超时 ({timeout}s)")
        except ConnectionError as e:
            self._connected = False
            logger.error(f"MCP 连接断开: {e}")
            return MCPResult(success=False, error=f"连接断开: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"MCP 响应解析失败: {e}")
            return MCPResult(success=False, error=f"响应解析失败: {e}")
        except Exception as e:
            logger.error(f"MCP 调用异常: {method} - {e}")
            return MCPResult(success=False, error=str(e))

    def _infer_timeout(self, method: str, params: Optional[Dict]) -> float:
        """根据方法类型推断超时时间"""
        # 长时间运行的方法
        if method in ("wait_task", "run_command"):
            if params:
                explicit_timeout = params.get("timeout")
                if explicit_timeout:
                    # params.timeout 是毫秒，转秒 + 额外余量
                    return explicit_timeout / 1000 + 10
            return 120.0  # 默认 2 分钟

        # 搜索操作可能较慢
        if method in ("find_files", "search_text", "find_symbol"):
            return 90.0

        # 其他方法使用默认超时
        return self.default_timeout

    def _parse_response(self, response: Dict) -> MCPResult:
        """解析 JSON-RPC 响应"""
        # 错误响应
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                error_msg = error.get("message", str(error))
                error_data = error.get("data")
                if error_data:
                    error_msg += f" | {error_data}"
            else:
                error_msg = str(error)
            return MCPResult(success=False, error=error_msg)

        # 成功响应
        result = response.get("result", {})
        if isinstance(result, dict):
            status = result.get("status", "success")
            if status == "error":
                return MCPResult(
                    success=False,
                    error=result.get("error", "未知错误"),
                    data=result.get("data")
                )
            return MCPResult(
                success=True,
                data=result.get("data", result)
            )

        # 非字典结果
        return MCPResult(success=True, data={"result": result})

    async def ping(self) -> bool:
        """健康检查"""
        result = await self.call("ping", timeout=5.0)
        return result.success

    async def ensure_connected(self) -> bool:
        """确保连接可用，断开则重连"""
        if self.connected:
            return True
        return await self.connect()

    async def set_workspace(self, root_path: str) -> bool:
        """设置 Axon 工作目录"""
        result = await self.call("set_workspace", {
            "root_path": root_path,
        }, timeout=10.0)
        if result.success:
            logger.info(f"MCP 工作目录: {root_path}")
        else:
            logger.warning(f"MCP 设置工作目录失败: {result.error}")
        return result.success
