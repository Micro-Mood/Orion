"""
Orion LLM 客户端
=================

异步 LLM 调用，支持 OpenAI 兼容 API。
FIFO 模型降级: flash → turbo → plus，失败时自动切换。
支持流式和非流式输出。
指数退避重试。
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Token 用量"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """LLM 响应"""
    content: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    reasoning: str = ""


@dataclass
class StreamChunk:
    """流式响应块"""
    content: str
    model: str
    finish_reason: str = ""
    usage: Optional[LLMUsage] = None


class LLMClient:
    """
    异步 LLM 客户端

    特性:
    - OpenAI 兼容 API (百炼/DeepSeek/Kimi)
    - FIFO 模型降级 (flash → turbo → plus)
    - 流式和非流式输出
    - 指数退避重试
    - Token 用量追踪
    """

    def __init__(self, api_key: str, base_url: str, models: List[str],
                 temperature: float = 0.7, timeout: int = 120,
                 max_retries: int = 3):
        # API Key 允许为空或 placeholder，调用时再验证
        if not models:
            raise ValueError("模型列表不能为空")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.models = models
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries

        self._model_index = 0
        self.last_usage = LLMUsage()
        self.total_usage = LLMUsage()

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    @property
    def current_model(self) -> str:
        """当前使用的模型"""
        return self.models[self._model_index]

    def _check_api_key(self):
        """验证 API Key 是否已配置"""
        if not self.api_key or self.api_key == "placeholder":
            raise LLMClientError(
                "API Key 未配置。请点击左下角齿轮图标，在设置中配置 API Key。"
            )

    # ==================== 非流式调用 ====================

    async def chat(self, messages: List[Dict[str, str]],
                   temperature: Optional[float] = None) -> LLMResponse:
        """
        调用 LLM Chat API（非流式）

        Args:
            messages: 消息列表 [{"role": "...", "content": "..."}]
            temperature: 温度覆盖

        Returns:
            LLMResponse 包含内容、模型名、用量

        Raises:
            LLMError: 所有模型都失败
        """
        self._check_api_key()
        last_error = None

        for model_offset in range(len(self.models)):
            model_idx = (self._model_index + model_offset) % len(self.models)
            model = self.models[model_idx]

            try:
                response = await self._call_with_retry(
                    model, messages, temperature
                )
                self._model_index = model_idx
                return response

            except LLMRateLimitError as e:
                logger.warning(f"模型 {model} 限流: {e}")
                last_error = e
                continue

            except LLMServerError as e:
                logger.warning(f"模型 {model} 服务错误: {e}")
                last_error = e
                continue

            except LLMTimeoutError as e:
                logger.warning(f"模型 {model} 超时: {e}")
                last_error = e
                continue

            except LLMClientError:
                raise

        raise LLMError(f"所有模型均不可用: {last_error}")

    async def _call_with_retry(self, model: str,
                                messages: List[Dict[str, str]],
                                temperature: Optional[float]) -> LLMResponse:
        """带重试的单模型调用"""
        last_error = None

        for attempt in range(self.max_retries):
            try:
                return await self._call_api(model, messages, temperature)

            except LLMRateLimitError:
                raise

            except LLMClientError:
                raise

            except (LLMServerError, LLMTimeoutError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = min(2 ** attempt, 10)
                    logger.info(f"重试 {attempt + 1}/{self.max_retries}，等待 {delay}s...")
                    await asyncio.sleep(delay)

        raise last_error

    async def _call_api(self, model: str,
                        messages: List[Dict[str, str]],
                        temperature: Optional[float]) -> LLMResponse:
        """单次 API 调用（非流式）"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": False,
        }

        try:
            response = await self._client.post(url, json=payload)
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"请求超时: {e}")
        except httpx.ConnectError as e:
            raise LLMServerError(f"连接失败: {e}")
        except httpx.HTTPError as e:
            raise LLMServerError(f"HTTP 错误: {e}")
        except RuntimeError as e:
            if "closed" in str(e).lower():
                await self._recreate_client()
                raise LLMServerError(f"HTTP 客户端已关闭，已重建: {e}")
            raise

        status = response.status_code

        if status == 200:
            return self._parse_response(model, response.json())

        if status == 429:
            body = self._safe_response_body(response)
            raise LLMRateLimitError(f"429 限流: {body}")

        if 400 <= status < 500:
            body = self._safe_response_body(response)
            raise LLMClientError(f"{status}: {body}")

        body = self._safe_response_body(response)
        raise LLMServerError(f"{status}: {body}")

    # ==================== 流式调用 ====================

    async def chat_stream(self, messages: List[Dict[str, str]],
                          temperature: Optional[float] = None):
        """
        流式调用 LLM Chat API

        FIFO 模型降级: 连接阶段失败则切换模型。
        流式中途失败则报错 (无法切换)。

        Yields:
            StreamChunk 逐块输出
        """
        self._check_api_key()
        last_error = None

        for model_offset in range(len(self.models)):
            model_idx = (self._model_index + model_offset) % len(self.models)
            model = self.models[model_idx]

            stream_gen = self._stream_api(model, messages, temperature)
            started = False

            try:
                async for chunk in stream_gen:
                    if not started:
                        started = True
                        self._model_index = model_idx
                    yield chunk
                return  # 流式完成

            except (LLMRateLimitError, LLMServerError, LLMTimeoutError) as e:
                if started:
                    raise LLMError(f"流式传输中断: {e}")
                last_error = e
                logger.warning(f"模型 {model} 流式失败: {e}")
                continue

            except LLMClientError:
                raise

            except RuntimeError as e:
                # httpx client 被关闭 (uvicorn reload 等场景)
                if "closed" in str(e).lower():
                    await self._recreate_client()
                    raise LLMServerError(f"HTTP 客户端已关闭，已重建: {e}")
                raise

            finally:
                await stream_gen.aclose()

        raise LLMError(f"所有模型均不可用: {last_error}")

    async def _stream_api(self, model: str,
                          messages: List[Dict[str, str]],
                          temperature: Optional[float]):
        """单模型流式 API 调用"""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        try:
            async with self._client.stream("POST", url, json=payload) as response:
                status = response.status_code

                if status != 200:
                    body = (await response.aread()).decode(
                        "utf-8", errors="replace"
                    )[:500]
                    if status == 429:
                        raise LLMRateLimitError(f"429: {body}")
                    elif 400 <= status < 500:
                        raise LLMClientError(f"{status}: {body}")
                    else:
                        raise LLMServerError(f"{status}: {body}")

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        return

                    try:
                        data = json.loads(data_str)
                        choices = data.get("choices", [])

                        if not choices:
                            # 可能是最终 usage 块
                            usage_data = data.get("usage")
                            if usage_data:
                                self._update_usage(usage_data)
                            continue

                        delta = choices[0].get("delta", {})
                        content = delta.get("content", "")
                        finish_reason = choices[0].get("finish_reason")

                        usage_data = data.get("usage")
                        usage = None
                        if usage_data:
                            usage = LLMUsage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0),
                                completion_tokens=usage_data.get("completion_tokens", 0),
                                total_tokens=usage_data.get("total_tokens", 0),
                            )
                            self._update_usage(usage_data)

                        if content:
                            yield StreamChunk(
                                content=content,
                                model=model,
                                finish_reason=finish_reason or "",
                                usage=usage,
                            )

                    except json.JSONDecodeError:
                        continue

        except httpx.TimeoutException as e:
            raise LLMTimeoutError(f"流式超时: {e}")
        except httpx.ConnectError as e:
            raise LLMServerError(f"连接失败: {e}")
        except (httpx.HTTPError, OSError) as e:
            raise LLMServerError(f"流式错误: {e}")

    # ==================== 辅助方法 ====================

    def _parse_response(self, model: str, data: Dict[str, Any]) -> LLMResponse:
        """解析 API 响应"""
        choices = data.get("choices", [])
        if not choices:
            raise LLMServerError("API 返回空 choices")

        message = choices[0].get("message", {})
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")

        usage_data = data.get("usage", {})
        usage = LLMUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )
        self._update_usage(usage_data)

        return LLMResponse(
            content=content,
            model=model,
            usage=usage,
            reasoning=reasoning,
        )

    def _update_usage(self, usage_data: dict):
        """更新用量追踪"""
        if not usage_data:
            return
        usage = LLMUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )
        self.last_usage = usage
        self.total_usage.prompt_tokens += usage.prompt_tokens
        self.total_usage.completion_tokens += usage.completion_tokens
        self.total_usage.total_tokens += usage.total_tokens

    def _safe_response_body(self, response: httpx.Response) -> str:
        """安全获取响应体文本"""
        try:
            return response.text[:500]
        except Exception:
            return "(无法读取响应体)"

    def reset_model(self):
        """重置模型到第一个（手动恢复）"""
        self._model_index = 0

    async def _recreate_client(self):
        """重建 httpx 客户端 (client 被关闭后恢复)"""
        try:
            await self._client.aclose()
        except Exception:
            pass
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        logger.info("httpx 客户端已重建")

    def update_config(self, api_key: str = None, base_url: str = None,
                      models: list = None, temperature: float = None):
        """运行时更新配置 (设置页保存后调用)"""
        if api_key is not None:
            self.api_key = api_key
            self._client.headers["Authorization"] = f"Bearer {api_key}"
        if base_url is not None:
            self.base_url = base_url.rstrip("/")
        if models is not None:
            self.models = models
            self._model_index = 0
        if temperature is not None:
            self.temperature = temperature

    async def close(self):
        """关闭客户端"""
        await self._client.aclose()


# ==================== 异常层级 ====================

class LLMError(Exception):
    """LLM 基础异常"""
    pass


class LLMClientError(LLMError):
    """客户端错误 (400 级别)，不应重试或降级"""
    pass


class LLMServerError(LLMError):
    """服务端错误 (500 级别)，可重试和降级"""
    pass


class LLMRateLimitError(LLMError):
    """限流错误 (429)，应降级到下一个模型"""
    pass


class LLMTimeoutError(LLMError):
    """超时错误，可重试和降级"""
    pass
