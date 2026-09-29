"""模型调用的 超时 / 重试 / 降级

- 超时：ChatOpenAI 的 timeout（非流式默认 30s，流式默认 60s）
- 重试：ChatOpenAI 的 max_retries（默认 2 次，SDK 自带指数退避）
- 降级：主模型整条调用失败（超时 / 限流 / 5xx / 连接失败）时自动切备用模型；
  调用参数（如 bind_tools 绑定的 tools）原样转发，保证 Function Calling 不失效；
  流式只有在"还没吐出第一个字"时才切换，避免用户看到半截答案再重来。
"""
import logging
import os
from typing import Any, AsyncIterator, Iterator, Optional

from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

from app.resilience import CircuitOpenError, get_breaker

logger = logging.getLogger("jisu.model")

# 单次模型调用超时（秒）
MODEL_TIMEOUT_SECONDS = float(os.getenv("MODEL_TIMEOUT_SECONDS", "30"))
MODEL_STREAM_TIMEOUT_SECONDS = float(os.getenv("MODEL_STREAM_TIMEOUT_SECONDS", "60"))
# 单次模型调用重试次数（不含第一次）
MODEL_MAX_RETRIES = int(os.getenv("MODEL_MAX_RETRIES", "2"))

# 备用模型（不配置则不启用降级）
FALLBACK_MODEL_NAME = os.getenv("FALLBACK_MODEL_NAME")
FALLBACK_BASE_URL = os.getenv("FALLBACK_BASE_URL") or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
FALLBACK_API_KEY = os.getenv("FALLBACK_API_KEY")
FALLBACK_TIMEOUT_SECONDS = float(os.getenv("FALLBACK_TIMEOUT_SECONDS", "30"))

# 熔断器名字：主模型与备用模型各自独立计数，备用模型挂了不会再无限切过去
PRIMARY_BREAKER = "llm:chat"
FALLBACK_BREAKER = "llm:chat:fallback"


class ResilientChatOpenAI(ChatOpenAI):
    """主模型失败时自动降级到备用模型的 ChatOpenAI

    fallback 由 create_model 注入；调用参数会原样转发给备用模型。
    """

    fallback: Optional[Any] = None

    def _fallback_model(self) -> Optional[ChatOpenAI]:
        return self.fallback if isinstance(self.fallback, ChatOpenAI) else None

    # ── 非流式 ────────────────────────────────────────────────
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        backup = self._fallback_model()
        try:
            with get_breaker(PRIMARY_BREAKER).guard():
                return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except CircuitOpenError:
            # 主模型熔断打开：不再等超时，直接走备用模型
            if backup is None:
                raise
            logger.warning("主模型 %s 熔断打开，直接降级到备用模型 %s", self.model_name, backup.model_name)
            with get_breaker(FALLBACK_BREAKER).guard():
                return backup._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as err:
            if backup is None:
                raise
            logger.warning("主模型 %s 调用失败，降级到备用模型 %s：%s", self.model_name, backup.model_name, err)
            with get_breaker(FALLBACK_BREAKER).guard():
                return backup._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        backup = self._fallback_model()
        try:
            async with get_breaker(PRIMARY_BREAKER).aguard():
                return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except CircuitOpenError:
            if backup is None:
                raise
            logger.warning("主模型 %s 熔断打开，直接降级到备用模型 %s", self.model_name, backup.model_name)
            async with get_breaker(FALLBACK_BREAKER).aguard():
                return await backup._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as err:
            if backup is None:
                raise
            logger.warning("主模型 %s 调用失败，降级到备用模型 %s：%s", self.model_name, backup.model_name, err)
            async with get_breaker(FALLBACK_BREAKER).aguard():
                return await backup._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    # ── 流式：只有在还没吐出任何内容时才降级 ──────────────────
    def _stream(self, *args: Any, **kwargs: Any) -> Iterator[ChatGenerationChunk]:
        backup = self._fallback_model()
        if backup is None:
            yield from super()._stream(*args, **kwargs)
            return

        breaker = get_breaker(PRIMARY_BREAKER)
        allowed, reason = breaker.allow()
        if not allowed:
            # 熔断打开：连第一个 token 都不用等了，直接换备用模型
            logger.warning("主模型 %s 熔断打开（%s），流式直接使用备用模型", self.model_name, reason)
            yield from backup._stream(*args, **kwargs)
            return

        started = False
        try:
            for chunk in super()._stream(*args, **kwargs):
                if not started:
                    breaker.record_success()   # 能吐出第一个 chunk 说明模型是活的
                started = True
                yield chunk
        except Exception as err:
            if not started:
                # 只有"一个字都没吐"才算主模型故障；已经输出了就不算（避免把用户侧中断算进去）
                breaker.record_failure(err)
            if started:
                raise
            logger.warning("主模型 %s 流式调用失败，降级到备用模型 %s：%s", self.model_name, backup.model_name, err)
            yield from backup._stream(*args, **kwargs)

    async def _astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        backup = self._fallback_model()
        if backup is None:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk
            return

        breaker = get_breaker(PRIMARY_BREAKER)
        allowed, reason = breaker.allow()
        if not allowed:
            logger.warning("主模型 %s 熔断打开（%s），流式直接使用备用模型", self.model_name, reason)
            async for chunk in backup._astream(*args, **kwargs):
                yield chunk
            return

        started = False
        try:
            async for chunk in super()._astream(*args, **kwargs):
                if not started:
                    breaker.record_success()
                started = True
                yield chunk
        except Exception as err:
            if not started:
                breaker.record_failure(err)
            if started:
                raise
            logger.warning("主模型 %s 流式调用失败，降级到备用模型 %s：%s", self.model_name, backup.model_name, err)
            async for chunk in backup._astream(*args, **kwargs):
                yield chunk


def make_fallback_model(*, streaming: bool, temperature: float) -> Optional[ChatOpenAI]:
    """构造备用模型；没配置 FALLBACK_MODEL_NAME / FALLBACK_API_KEY 时返回 None（不启用降级）"""
    if not (FALLBACK_MODEL_NAME and FALLBACK_API_KEY):
        return None

    params: dict[str, Any] = {
        "model": FALLBACK_MODEL_NAME,
        "api_key": FALLBACK_API_KEY,
        "base_url": FALLBACK_BASE_URL,
        "temperature": temperature,
        "streaming": streaming,
        "timeout": FALLBACK_TIMEOUT_SECONDS,
        "max_retries": MODEL_MAX_RETRIES,
    }
    if streaming:
        params["stream_usage"] = True

    try:
        return ChatOpenAI(**params)
    except TypeError:
        params.pop("stream_usage", None)
        return ChatOpenAI(**params)
