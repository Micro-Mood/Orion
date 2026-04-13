"""Orion — 启动入口"""

import atexit
import asyncio
import logging
import sys

import uvicorn


def setup_logging():
    """配置日志"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


if __name__ == "__main__":
    setup_logging()

    from pathlib import Path
    from config import get_config
    from axon_manager import AxonManager

    cfg = get_config()

    # src/ 目录 (只监听源码变化, 不监听 Axon 工作区的文件操作)
    SRC_DIR = str(Path(__file__).resolve().parent)

    # ---- Axon 子进程管理 ----
    axon_mgr = None

    axon_mgr = AxonManager(
        host=cfg.axon.host,
        port=cfg.axon.port,
        workspace=cfg.axon.workspace or cfg.get_working_directory(),
    )

    print(f"Orion 启动中...")
    print(f"  正在拉起 Axon MCP Server...")

    ok = asyncio.run(axon_mgr.start())
    if ok:
        if axon_mgr.is_external:
            print(f"  Axon: {cfg.axon.host}:{cfg.axon.port} (外部进程)")
        else:
            print(f"  Axon: {cfg.axon.host}:{cfg.axon.port} "
                  f"(PID={axon_mgr._process.pid})")
    else:
        print(f"  [!] Axon 启动失败，工具调用将不可用")
        print(f"      请手动启动: python -m src --port {cfg.axon.port}")

    # 注册退出清理
    atexit.register(axon_mgr.stop_sync)

    # 将 axon_mgr 存到 axon_manager 模块级变量，供 server.py 访问
    import axon_manager
    axon_manager._instance = axon_mgr

    print(f"  地址: http://{cfg.server.host}:{cfg.server.port}")
    print(f"  模型: {', '.join(cfg.llm.models)}")
    print(f"  API Key: {'已配置' if cfg.llm.api_key else '未配置 [!]'}")
    print(f"  工作目录: {cfg.get_working_directory()}")

    uvicorn.run(
        "server:app",
        host=cfg.server.host,
        port=cfg.server.port,
        reload=True,
        reload_dirs=[SRC_DIR],
        log_level="info",
    )
