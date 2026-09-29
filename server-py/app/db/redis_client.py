"""Redis 客户端 + 会话缓存

会话上下文以 session_id 为 key 存放在 Redis：
- key   : session:{session_id}
- value : {"owner": "U-100", "messages": [{"role": ..., "content": ...}]}
- TTL   : SESSION_TTL_SECONDS，默认 1800 秒（30 分钟），每次写入自动续期

**为什么 value 里要存 owner**：会话里装着用户问过的订单、看到过的数据。
如果只按 session_id 取会话，那么换一个用户身份（或把 session_id 猜/抄过来）就能
读走别人上一轮的上下文 —— 上下文进了提示词，等于订单数据被读走。
所以读取时用 owner 做归属校验：不是本人的会话，一律当"没有会话"（fail closed）。

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

from app.resilience import get_breaker
from app.security import identity

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
    """会话缓存 key：直接使用 session_id（归属校验放在 value 的 owner 字段上）"""
    return f"session:{session_id}"


def _decode(raw: str) -> tuple[str, list]:
    """解析缓存内容 → (owner, messages)

    兼容旧格式（纯数组）：当作"没有归属信息"的会话处理 —— 对已登录用户来说
    这种会话一律不认（宁可让他重开一轮，也不能赌它是本人的）。
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return "", []
    if isinstance(payload, list):
        return "", payload
    if isinstance(payload, dict):
        messages = payload.get("messages")
        return str(payload.get("owner") or ""), (messages if isinstance(messages, list) else [])
    return "", []


def _encode(history: list, owner: str) -> str:
    return json.dumps({"owner": owner or "", "messages": history[-SESSION_MAX_MESSAGES:]},
                      ensure_ascii=False)


async def ping() -> bool:
    """健康检查用"""
    try:
        async with get_circuit().aguard():
            return bool(await get_redis().ping())
    except Exception as err:
        logger.warning("Redis 不可用: %s", err)
        return False


async def session_owner(session_id: str) -> str:
    """这个会话是谁的（读不到时返回空串）"""
    if not session_id:
        return ""
    try:
        async with get_circuit().aguard():
            raw = await get_redis().get(session_key(session_id))
        return _decode(raw)[0] if raw else ""
    except Exception as err:
        logger.warning("读取会话归属失败: %s", err)
        return ""


async def get_history(session_id: str, owner: str | None = None) -> list[dict]:
    """读取会话历史；Redis 不可用或没有数据时返回空列表，由调用方降级

    owner 不为 None 时做归属校验：不是这个用户的会话，直接当没有（并记一条权限事件）。
    owner=None 只有管理后台/可观测接口在用（管理员看全部会话）。
    """
    if not session_id:
        return []
    try:
        async with get_circuit().aguard():
            raw = await get_redis().get(session_key(session_id))
        if not raw:
            return []
        stored_owner, history = _decode(raw)
        if owner is not None and stored_owner != (owner or ""):
            # 会话是别人的：一个字都不返回，并且留痕（可能是 session_id 被抄走了）
            identity.record_incident("session_owner_mismatch", session_id=session_id,
                                     actor=owner or "anonymous", owner=stored_owner or "unknown")
            return []
        return history
    except Exception as err:
        logger.warning("读取会话缓存失败，降级为请求体历史: %s", err)
        return []


async def save_history(session_id: str, history: list[dict], owner: str = "") -> None:
    """写入会话历史（连同归属），TTL 续期为 30 分钟"""
    if not session_id:
        return
    try:
        payload = _encode(history, owner)
        async with get_circuit().aguard():
            await get_redis().set(session_key(session_id), payload, ex=SESSION_TTL_SECONDS)
    except Exception as err:
        logger.warning("写入会话缓存失败: %s", err)


async def append_turn(session_id: str, user_message: str, assistant_message: str = "",
                      owner: str = "") -> None:
    """追加一轮问答并刷新 TTL（只能追加到自己的会话上）"""
    if not session_id:
        return
    history = await get_history(session_id, owner=owner)
    history.append({"role": "user", "content": user_message})
    if assistant_message:
        history.append({"role": "assistant", "content": assistant_message})
    await save_history(session_id, history, owner=owner)


async def clear_session(session_id: str, owner: str | None = None) -> bool:
    """删除会话缓存；给了 owner 就只能删自己的（删别人的返回 False 并留痕）"""
    if not session_id:
        return False
    if owner is not None:
        stored_owner = await session_owner(session_id)
        if stored_owner and stored_owner != owner:
            identity.record_incident("session_clear_denied", session_id=session_id,
                                     actor=owner or "anonymous", owner=stored_owner)
            return False
    try:
        async with get_circuit().aguard():
            await get_redis().delete(session_key(session_id))
        return True
    except Exception as err:
        logger.warning("清空会话缓存失败: %s", err)
        return False


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
