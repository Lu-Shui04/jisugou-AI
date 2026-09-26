"""提示词安全：结果缓存 + 事件日志 + 统计

都走 Redis，Redis 不可用时退化为进程内存（只影响缓存命中率和后台展示，
不影响拦截本身）。

缓存的意义：同样的输入（含被反复试探的攻击样本）只花一次小模型的 token。
"""
import hashlib
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from app.db import redis_client

logger = logging.getLogger("jisu.security")

CACHE_TTL_SECONDS = int(os.getenv("SECURITY_CACHE_TTL", "86400"))       # 同样输入 24h 内不重复判
EVENT_TTL_SECONDS = int(os.getenv("SECURITY_EVENT_TTL", str(7 * 24 * 3600)))
MAX_EVENTS = int(os.getenv("SECURITY_MAX_EVENTS", "200"))               # 后台展示的最近事件数

# 缓存 key 带版本号：判定逻辑一改（短回话要连上文一起判）就得换版本，
# 否则旧逻辑在"孤立一句话"上下文里判出来的攻击结论会继续生效 ——
# 线上"两个都要"被卡住就是这么来的：判错一次，缓存 24h，用户再怎么补都没用。
CACHE_KEY = "sec:cache:v2:{digest}"
EVENT_KEY = "sec:events"
STATS_KEY = "sec:stats"

_memory_cache: dict[str, dict] = {}
_memory_events: deque = deque(maxlen=MAX_EVENTS)
_memory_stats: dict[str, int] = {}


def digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:24]


# ── 判定缓存 ────────────────────────────────────────────────────
async def get_cached(text: str) -> Optional[dict]:
    key = CACHE_KEY.format(digest=digest(text))
    try:
        raw = await redis_client.get_redis().get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    item = _memory_cache.get(key)
    if item and item.get("expire_at", 0) > time.time():
        return item.get("value")
    return None


async def set_cached(text: str, value: dict) -> None:
    key = CACHE_KEY.format(digest=digest(text))
    payload = json.dumps(value, ensure_ascii=False)
    try:
        await redis_client.get_redis().set(key, payload, ex=CACHE_TTL_SECONDS)
    except Exception:
        pass
    if len(_memory_cache) > 500:
        _memory_cache.clear()
    _memory_cache[key] = {"value": value, "expire_at": time.time() + CACHE_TTL_SECONDS}


# ── 统计 ────────────────────────────────────────────────────────
async def bump(field: str, amount: int = 1) -> None:
    try:
        await redis_client.get_redis().hincrby(STATS_KEY, field, amount)
    except Exception:
        pass
    _memory_stats[field] = _memory_stats.get(field, 0) + amount


def bump_sync(field: str, amount: int = 1) -> None:
    """同步场景（如在 LLM 链内部）累加统计：先记内存，再尽力写 Redis"""
    _memory_stats[field] = _memory_stats.get(field, 0) + amount
    try:
        import asyncio

        asyncio.get_running_loop().create_task(bump(field, amount))
    except Exception:
        pass


async def get_stats() -> dict:
    """Redis 可用时以 Redis 为准（哪怕为空），只有它挂了才退回内存

    否则刚清空的统计会被本进程的旧镜像"复活"（多 worker 下尤其明显）。
    """
    try:
        raw = await redis_client.get_redis().hgetall(STATS_KEY)
        return {k: int(v) for k, v in (raw or {}).items()}
    except Exception:
        pass
    return dict(_memory_stats)


async def reset_stats() -> None:
    try:
        await redis_client.get_redis().delete(STATS_KEY)
    except Exception:
        pass
    _memory_stats.clear()


# ── 事件日志（只记拦截/异常，正常放行不记，省空间）────────────────
async def log_event(event: dict) -> None:
    record = {
        "ts": int(time.time() * 1000),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **event,
    }
    _memory_events.appendleft(record)
    try:
        client = redis_client.get_redis()
        pipe = client.pipeline(transaction=False)
        pipe.lpush(EVENT_KEY, json.dumps(record, ensure_ascii=False))
        pipe.ltrim(EVENT_KEY, 0, MAX_EVENTS - 1)
        pipe.expire(EVENT_KEY, EVENT_TTL_SECONDS)
        await pipe.execute()
    except Exception as err:
        logger.warning("安全事件写入 Redis 失败: %s", err)


async def list_events(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), MAX_EVENTS))
    try:
        raw = await redis_client.get_redis().lrange(EVENT_KEY, 0, limit - 1)
        return [json.loads(item) for item in (raw or [])]
    except Exception:
        pass
    return list(_memory_events)[:limit]


async def clear_events() -> int:
    count = len(_memory_events)
    _memory_events.clear()
    try:
        count = int(await redis_client.get_redis().llen(EVENT_KEY))
        await redis_client.get_redis().delete(EVENT_KEY)
    except Exception:
        pass
    return count
