"""Redis 客户端 + 会话缓存

会话上下文以 session_id 为 key 存放在 Redis：
- key   : session:{session_id}
- value : JSON 数组 [{"role": "user"|"assistant", "content": "..."}]
- TTL   : SESSION_TTL_SECONDS，默认 1800 秒（30 分钟），每次写入自动续期

Redis 不可用时自动降级：读写失败只记日志，接口回退到请求体里的 history，
不影响主流程。
"""
import json
import logging
import os
from typing import Optional

import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.retry import Retry

from app.resilience import CircuitOpenError, get_breaker

logger = logging.getLogger("jisu.redis")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

# 会话缓存 TTL：30 分钟
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
# 单个会话保留的最大消息条数，防止上下文无限膨胀
SESSION_MAX_MESSAGES = int(os.getenv("SESSION_MAX_MESSAGES", "20"))

_client: Optional[aioredis.Redis] = None

# Redis 挂了的时候要"快失败"：默认的重试策略会指数退避，
# 实测本机把 Redis 停掉后单次 GET 要等 14.6 秒 —— 每个请求都要读几次缓存，
# 等于整个站点被一个挂掉的 Redis 拖住。所以这里不重试 + 短超时，
# 失败立刻走各自的降级逻辑（会话退回请求体 history、缓存直接不用）。
REDIS_CONNECT_TIMEOUT = float(os.getenv("REDIS_CONNECT_TIMEOUT", "1.0"))
REDIS_SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", "2.0"))

# Redis 熔断：连续失败到阈值后，一段时间内不再尝试连接，直接降级（0 等待）
REDIS_BREAKER = "cache:redis"


def get_redis() -> aioredis.Redis:
    """懒加载 Redis 连接（复用连接池）"""
    global _client
    if _client is None:
        _client = aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[],
        )
    return _client


def get_circuit():
    """Redis 熔断器（供各处复用；Redis 不可用时也必须是纯内存对象，不能再依赖 Redis）"""
    return get_breaker(REDIS_BREAKER)


def session_key(session_id: str) -> str:
    """会话缓存 key：直接使用 session_id"""
    return f"session:{session_id}"


async def ping() -> bool:
    """健康检查用"""
    try:
        async with get_circuit().aguard():
            return bool(await get_redis().ping())
    except Exception as err:
        logger.warning("Redis 不可用: %s", err)
        return False


async def get_history(session_id: str) -> list[dict]:
    """读取会话历史；Redis 不可用或没有数据时返回空列表，由调用方降级"""
    if not session_id:
        return []
    try:
        async with get_circuit().aguard():
            raw = await get_redis().get(session_key(session_id))
        if not raw:
            return []
        history = json.loads(raw)
        return history if isinstance(history, list) else []
    except Exception as err:
        logger.warning("读取会话缓存失败，降级为请求体历史: %s", err)
        return []


async def save_history(session_id: str, history: list[dict]) -> None:
    """写入会话历史，TTL 续期为 30 分钟"""
    if not session_id:
        return
    try:
        payload = json.dumps(history[-SESSION_MAX_MESSAGES:], ensure_ascii=False)
        async with get_circuit().aguard():
            await get_redis().set(session_key(session_id), payload, ex=SESSION_TTL_SECONDS)
    except Exception as err:
        logger.warning("写入会话缓存失败: %s", err)


async def append_turn(session_id: str, user_message: str, assistant_message: str = "") -> None:
    """追加一轮问答并刷新 TTL"""
    if not session_id:
        return
    history = await get_history(session_id)
    history.append({"role": "user", "content": user_message})
    if assistant_message:
        history.append({"role": "assistant", "content": assistant_message})
    await save_history(session_id, history)


async def clear_session(session_id: str) -> None:
    """删除会话缓存"""
    if not session_id:
        return
    try:
        async with get_circuit().aguard():
            await get_redis().delete(session_key(session_id))
    except Exception as err:
        logger.warning("清空会话缓存失败: %s", err)


async def session_ttl(session_id: str) -> int:
    """剩余存活秒数，-2 表示 key 不存在，-1 表示 Redis 不可用"""
    if not session_id:
        return -2
    try:
        async with get_circuit().aguard():
            return int(await get_redis().ttl(session_key(session_id)))
    except Exception as err:
        logger.warning("查询会话 TTL 失败: %s", err)
        return -1
