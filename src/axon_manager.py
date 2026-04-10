"""
Axon 子进程管理
===============

负责 Axon MCP Server 的启动、监控、重启和停止。
Orion 启动时自动拉起 Axon 子进程，退出时优雅关闭。
"""

import asyncio
import logging
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Axon 子模块路径: Orion/axon/
AXON_DIR = Path(__file__).resolve().parent.parent / "axon"


class AxonManager:
    """
    Axon 子进程生命周期管理

    - start(): 启动 Axon 并等待端口就绪
    - stop(): 优雅关闭 Axon 子进程
    - restart(): 重启
    - is_running: 进程是否存活
    - is_external: 是否检测到外部已启动的 Axon（不由本管理器控制）
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9100,
                 workspace: str = "",
                 max_restarts: int = 3,
                 ready_timeout: float = 10.0):
        self.host = host
        self.port = port
        self.workspace = workspace
        self.max_restarts = max_restarts
        self.ready_timeout = ready_timeout

        self._process: Optional[subprocess.Popen] = None
        self._restart_count = 0
        self._monitor_task: Optional[asyncio.Task] = None
        self._stopped = False  # 标记是否主动停止（区分崩溃）
        self._external = False  # 外部已启动的 Axon

    @property
    def is_running(self) -> bool:
        """子进程是否存活"""
        if self._external:
            return self._check_port()
        return self._process is not None and self._process.poll() is None

    @property
    def is_external(self) -> bool:
        """是否由外部管理"""
        return self._external

    async def start(self) -> bool:
        """
        启动 Axon 子进程

        1. 检查端口是否已被占用（外部 Axon 已在运行）
        2. 检查 axon 子模块是否存在
        3. spawn 子进程
        4. 等待 TCP 端口就绪
        5. 启动后台监控

        Returns:
            True 表示 Axon 已就绪（不论是新启动还是外部已运行）
        """
        self._stopped = False

        # 1. 检查端口是否已被占用
        if self._check_port():
            logger.info(
                f"Axon 已在 {self.host}:{self.port} 运行（外部进程）"
            )
            self._external = True
            return True

        # 2. 检查子模块
        axon_main = AXON_DIR / "src" / "__main__.py"
        if not axon_main.exists():
            logger.error(
                f"Axon 子模块不存在: {AXON_DIR}\n"
                "  请执行: git submodule update --init"
            )
            return False

        # 3. 启动子进程
        self._external = False
        ok = self._spawn()
        if not ok:
            return False

        # 4. 等待端口就绪
        ready = await self._wait_ready()
        if not ready:
            logger.error("Axon 启动超时，未能在端口上监听")
            self.stop_sync()
            return False

        # 5. 启动后台监控
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        logger.info(f"Axon 已启动 (PID={self._process.pid})")
        return True

    def _spawn(self) -> bool:
        """创建 Axon 子进程"""
        cmd = [
            sys.executable, "-m", "src",
            "--host", self.host,
            "--port", str(self.port),
        ]
        if self.workspace:
            cmd.extend(["--workspace", self.workspace])

        try:
            # 创建子进程，继承 stderr 供调试，stdout 不干扰
            creation_flags = 0
            if sys.platform == "win32":
                # Windows: 创建新进程组以便独立信号
                creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

            self._process = subprocess.Popen(
                cmd,
                cwd=str(AXON_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creation_flags,
            )
            return True
        except FileNotFoundError:
            logger.error(f"Python 解释器未找到: {sys.executable}")
            return False
        except Exception as e:
            logger.error(f"启动 Axon 失败: {e}")
            return False

    async def _wait_ready(self) -> bool:
        """轮询等待 Axon TCP 端口就绪"""
        interval = 0.3
        elapsed = 0.0

        while elapsed < self.ready_timeout:
            # 检查进程是否已退出
            if self._process and self._process.poll() is not None:
                rc = self._process.returncode
                stderr = ""
                try:
                    stderr = self._process.stderr.read().decode(
                        errors="replace")[:500]
                except Exception:
                    pass
                logger.error(
                    f"Axon 进程提前退出 (code={rc})\n{stderr}")
                return False

            if self._check_port():
                return True

            await asyncio.sleep(interval)
            elapsed += interval

        return False

    def _check_port(self) -> bool:
        """检查 TCP 端口是否可连接"""
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=0.5
            ):
                return True
        except (ConnectionRefusedError, OSError, TimeoutError):
            return False

    async def _monitor_loop(self):
        """后台监控: 检测 Axon 崩溃并自动重启"""
        try:
            while not self._stopped:
                await asyncio.sleep(3)

                if self._stopped or self._external:
                    break

                if self._process and self._process.poll() is not None:
                    rc = self._process.returncode
                    logger.warning(
                        f"Axon 进程意外退出 (code={rc})")

                    if self._restart_count < self.max_restarts:
                        self._restart_count += 1
                        logger.info(
                            f"正在重启 Axon "
                            f"({self._restart_count}/{self.max_restarts})..."
                        )
                        ok = self._spawn()
                        if ok:
                            ready = await self._wait_ready()
                            if ready:
                                logger.info(
                                    f"Axon 重启成功 "
                                    f"(PID={self._process.pid})")
                                continue
                        logger.error("Axon 重启失败")
                    else:
                        logger.error(
                            f"Axon 已连续崩溃 {self.max_restarts} 次，"
                            "停止重启")
                    break

        except asyncio.CancelledError:
            pass

    def stop_sync(self):
        """同步停止 Axon 子进程（用于 atexit 等非 async 场景）"""
        self._stopped = True

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()

        if self._external:
            logger.info("Axon 由外部管理，不执行停止")
            return

        if self._process is None:
            return

        if self._process.poll() is not None:
            self._process = None
            return

        pid = self._process.pid
        logger.info(f"正在停止 Axon (PID={pid})...")

        try:
            if sys.platform == "win32":
                # Windows: 发送 CTRL_BREAK_EVENT
                os.kill(self._process.pid, signal.CTRL_BREAK_EVENT)
            else:
                self._process.terminate()

            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Axon 未响应 TERM 信号，强制杀死")
                self._process.kill()
                self._process.wait(timeout=3)
        except Exception as e:
            logger.warning(f"停止 Axon 时出错: {e}")
            try:
                self._process.kill()
            except Exception:
                pass

        self._process = None
        logger.info("Axon 已停止")

    async def stop(self):
        """异步停止 Axon 子进程"""
        self.stop_sync()

    async def restart(self) -> bool:
        """重启 Axon"""
        await self.stop()
        self._restart_count = 0
        return await self.start()

    def update_config(self, host: str = None, port: int = None,
                      workspace: str = None):
        """更新配置（下次重启生效）"""
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        if workspace is not None:
            self.workspace = workspace
