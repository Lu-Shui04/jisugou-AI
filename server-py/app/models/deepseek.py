"""
模型封装
将 DeepSeek 封装为 LangChain ChatModel
DeepSeek 兼容 OpenAI 协议，使用 ChatOpenAI 并替换 base_url 即可

容错策略（见 app/models/resilience.py）：
- 超时：非流式 30s / 流式 60s，可用 MODEL_TIMEOUT_SECONDS 覆盖
- 重试：max_retries 默认 2 次，SDK 自带指数退避
- 降级：配置 FALLBACK_MODEL_NAME + FALLBACK_API_KEY 后，主模型失败自动切备用模型
"""
import os

from langchain_openai import ChatOpenAI

from app.resilience.model_fallback import (
    MODEL_MAX_RETRIES,
    MODEL_STREAM_TIMEOUT_SECONDS,
    MODEL_TIMEOUT_SECONDS,
    ResilientChatOpenAI,
    make_fallback_model,
)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-chat")


def create_model(**overrides) -> ChatOpenAI:
    """创建 DeepSeek 模型实例，可通过 overrides 覆盖默认参数，如 temperature=0, streaming=True"""
    params: dict = {
        "model": MODEL_NAME,
        "api_key": DEEPSEEK_API_KEY,
        "base_url": DEEPSEEK_BASE_URL,
        "temperature": 0.7,
        "streaming": False,
    }
    params.update(overrides)

    streaming = bool(params.get("streaming"))

    # 超时 / 重试
    params.setdefault("timeout", MODEL_STREAM_TIMEOUT_SECONDS if streaming else MODEL_TIMEOUT_SECONDS)
    params.setdefault("max_retries", MODEL_MAX_RETRIES)

    # 流式请求要带 token 用量，否则流式接口的 Token 统计为 0
    if streaming:
        params.setdefault("stream_usage", True)

    try:
        model = ResilientChatOpenAI(**params)
    except TypeError:
        # 兼容不支持 stream_usage 参数的旧版本 langchain-openai
        params.pop("stream_usage", None)
        model = ResilientChatOpenAI(**params)

    # 降级：主模型失败时切备用模型
    fallback = make_fallback_model(
        streaming=streaming,
        temperature=float(params.get("temperature", 0.7)),
    )
    if fallback is not None:
        model.fallback = fallback

    return model


# 默认导出一个标准实例（非流式）
model = create_model()

# 流式实例，用于 SSE 接口
streaming_model = create_model(streaming=True)
