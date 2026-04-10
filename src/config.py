"""
Orion 配置管理
==============

单例模式，支持 config.json + 环境变量覆盖。
优先级: 环境变量 > config.json > 默认值
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# 项目根目录: Orion/
BASE_DIR = Path(__file__).resolve().parent.parent

# 配置文件路径
CONFIG_PATH = BASE_DIR / "config.json"

# 默认工作空间: Orion/workspace/
DEFAULT_WORKSPACE = BASE_DIR / "workspace"


@dataclass
class LLMConfig:
    """LLM 配置"""
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    models: List[str] = field(default_factory=lambda: [
        "qwen-flash",
        "qwen-turbo",
        "qwen-plus",
    ])
    temperature: float = 0.7
    timeout: int = 120
    max_retries: int = 3


@dataclass
class AxonConfig:
    """Axon MCP Server 配置"""
    host: str = "127.0.0.1"
    port: int = 9100
    connect_timeout: float = 5.0
    call_timeout: float = 60.0
    auto_start: bool = True
    workspace: str = ""


@dataclass
class EngineConfig:
    """引擎配置"""
    max_history: int = 20
    max_iterations: int = 30
    working_directory: str = ""
    stream_chunk_size: int = 4
    stream_chunk_delay: float = 0.02


@dataclass
class ServerConfig:
    """服务器配置"""
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class Config:
    """全局配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    axon: AxonConfig = field(default_factory=AxonConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


class ConfigManager:
    """配置管理器（单例）"""

    _instance: Optional["ConfigManager"] = None

    def __new__(cls) -> "ConfigManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config = Config()
        self._load()

    @property
    def config(self) -> Config:
        return self._config

    @property
    def llm(self) -> LLMConfig:
        return self._config.llm

    @property
    def axon(self) -> AxonConfig:
        return self._config.axon

    @property
    def engine(self) -> EngineConfig:
        return self._config.engine

    @property
    def server(self) -> ServerConfig:
        return self._config.server

    def _load(self):
        """加载配置: config.json → 环境变量覆盖"""
        # 1. 从文件加载
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._apply_dict(raw)
            except (json.JSONDecodeError, IOError) as e:
                print(f"[Config] 配置文件加载失败: {e}")

        # 2. 环境变量覆盖
        env_map = {
            "ORION_API_KEY":       ("llm", "api_key"),
            "ORION_API_URL":       ("llm", "base_url"),
            "ORION_TEMPERATURE":   ("llm", "temperature", float),
            "ORION_AXON_HOST":     ("axon", "host"),
            "ORION_AXON_PORT":     ("axon", "port", int),
            "ORION_AXON_WORKSPACE": ("axon", "workspace"),
            "ORION_MAX_HISTORY":   ("engine", "max_history", int),
            "ORION_MAX_ITERATIONS": ("engine", "max_iterations", int),
            "ORION_WORKING_DIR":   ("engine", "working_directory"),
            "ORION_HOST":          ("server", "host"),
            "ORION_PORT":          ("server", "port", int),
        }

        for env_key, mapping in env_map.items():
            value = os.environ.get(env_key)
            if value is not None:
                section = mapping[0]
                attr = mapping[1]
                converter = mapping[2] if len(mapping) > 2 else str
                try:
                    section_obj = getattr(self._config, section)
                    setattr(section_obj, attr, converter(value))
                except (ValueError, AttributeError):
                    pass

        # 3. 验证必填项
        if not self._config.llm.api_key:
            print("[Config] 警告: API Key 未配置，请设置 ORION_API_KEY 或 config.json")

    def _apply_dict(self, raw: dict):
        """从字典更新配置"""
        llm = raw.get("llm", {})
        if llm:
            for k, v in llm.items():
                if hasattr(self._config.llm, k):
                    setattr(self._config.llm, k, v)

        axon = raw.get("axon", {})
        if axon:
            for k, v in axon.items():
                if hasattr(self._config.axon, k):
                    setattr(self._config.axon, k, v)

        engine = raw.get("engine", {})
        if engine:
            for k, v in engine.items():
                if hasattr(self._config.engine, k):
                    setattr(self._config.engine, k, v)

        server = raw.get("server", {})
        if server:
            for k, v in server.items():
                if hasattr(self._config.server, k):
                    setattr(self._config.server, k, v)

    def reload(self):
        """重新加载配置"""
        self._config = Config()
        self._load()

    def get_working_directory(self) -> str:
        """获取工作目录（绝对路径）

        优先级: engine.working_directory > axon.workspace > Orion/workspace/
        """
        cwd = self._config.engine.working_directory
        if cwd:
            return str(Path(cwd).resolve())
        cwd = self._config.axon.workspace
        if cwd:
            return str(Path(cwd).resolve())
        DEFAULT_WORKSPACE.mkdir(parents=True, exist_ok=True)
        return str(DEFAULT_WORKSPACE)

    def to_dict(self, mask_key: bool = True) -> dict:
        """序列化配置为字典 (发送给前端)"""
        cfg = self._config
        return {
            "llm": {
                "api_key": self._mask_api_key() if mask_key else cfg.llm.api_key,
                "base_url": cfg.llm.base_url,
                "models": list(cfg.llm.models),
                "temperature": cfg.llm.temperature,
                "timeout": cfg.llm.timeout,
                "max_retries": cfg.llm.max_retries,
            },
            "axon": {
                "host": cfg.axon.host,
                "port": cfg.axon.port,
                "connect_timeout": cfg.axon.connect_timeout,
                "call_timeout": cfg.axon.call_timeout,
                "auto_start": cfg.axon.auto_start,
                "workspace": cfg.axon.workspace,
            },
            "engine": {
                "max_history": cfg.engine.max_history,
                "max_iterations": cfg.engine.max_iterations,
                "working_directory": cfg.engine.working_directory,
                "stream_chunk_size": cfg.engine.stream_chunk_size,
                "stream_chunk_delay": cfg.engine.stream_chunk_delay,
            },
            "server": {
                "host": cfg.server.host,
                "port": cfg.server.port,
            },
            "effective_cwd": self.get_working_directory(),
        }

    def _mask_api_key(self) -> str:
        """遮蔽 API Key 中间部分"""
        key = self._config.llm.api_key
        if not key or len(key) < 8:
            return key
        return key[:4] + "****" + key[-4:]

    def save(self):
        """保存当前配置到 config.json"""
        data = self.to_dict(mask_key=False)
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def update_from_dict(self, data: dict):
        """从前端提交的字典更新配置 (自动跳过遮蔽的 API Key)"""
        llm = data.get("llm", {})
        if "api_key" in llm and "****" in str(llm.get("api_key", "")):
            llm.pop("api_key")
        self._apply_dict(data)


def get_config() -> ConfigManager:
    """获取全局配置管理器"""
    return ConfigManager()
