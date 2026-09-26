"""工具调用的 超时 / 重试 / 降级

工具背后通常是订单、物流这类外部系统，会超时、会抖动、会报错。
这里统一给工具加一层保护：

- 超时：超过 TOOL_TIMEOUT_SECONDS 就放弃这次调用（不拖住整条 Agent 链路）
- 重试：失败后按 0.3s、0.6s… 递增间隔重试，最多 TOOL_MAX_RETRIES 次
- 降级：最终仍失败时返回一段可读的 error JSON，让模型能告诉用户"稍后再试"，
  而不是把异常抛出去把整条链路打断
"""
import contextvars
import functools
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable

logger = logging.getLogger("jisu.tool")

TOOL_TIMEOUT_SECONDS = float(os.getenv("TOOL_TIMEOUT_SECONDS", "5"))
TOOL_MAX_RETRIES = int(os.getenv("TOOL_MAX_RETRIES", "2"))
TOOL_RETRY_BACKOFF_SECONDS = float(os.getenv("TOOL_RETRY_BACKOFF_SECONDS", "0.3"))


class ToolTimeoutError(TimeoutError):
    """工具调用超时"""


def _call_with_timeout(fn: Callable, args: tuple, kwargs: dict, timeout: float) -> Any:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # concurrent.futures 的新线程**不会**继承 ContextVar（它没有 asyncio 那套
        # "创建任务时复制上下文"的语义）。不显式 copy_context，工具函数里读到的
        # "当前登录用户"会退化成匿名 —— 权限校验会因此静默失效，所以必须带上。
        context = contextvars.copy_context()
        future = pool.submit(context.run, fn, *args, **kwargs)
        return future.result(timeout=timeout)
    except FuturesTimeout as err:
        raise ToolTimeoutError(f"工具调用超过 {timeout}s 未返回") from err
    finally:
        # 不等待超时线程结束，避免拖住请求
        pool.shutdown(wait=False)


def resilient_tool(name: str, timeout: float | None = None, retries: int | None = None):
    """给工具函数加上 超时 + 重试 + 失败降级"""

    def decorator(fn: Callable) -> Callable:
        attempts = (TOOL_MAX_RETRIES if retries is None else retries) + 1

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            last_error: Exception | None = None

            for attempt in range(attempts):
                try:
                    return _call_with_timeout(
                        fn, args, kwargs,
                        TOOL_TIMEOUT_SECONDS if timeout is None else timeout,
                    )
                except Exception as err:  # 超时 / 网络 / 业务异常统一处理
                    last_error = err
                    if attempt < attempts - 1:
                        delay = TOOL_RETRY_BACKOFF_SECONDS * (attempt + 1)
                        logger.warning(
                            "工具 %s 第 %s 次调用失败（%s），%.1fs 后重试",
                            name, attempt + 1, err, delay,
                        )
                        time.sleep(delay)

            logger.error("工具 %s 重试 %s 次后仍失败，走降级：%s", name, attempts - 1, last_error)
            return json.dumps(
                {
                    "error": f"{name} 暂时不可用，请稍后再试或联系人工客服 400-888-8888",
                    "degraded": True,
                },
                ensure_ascii=False,
            )

        return wrapper

    return decorator
