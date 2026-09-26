"""对话记录（审计日志）存储

每轮问答落一条记录，供管理员后台查看「不同用户与 AI 的聊天记录」，
记录里同时包含 RAG 的检索命中片段与重排（相似度打分 + 阈值过滤）明细。

Redis 结构：
    chatlog:turn:{trace_id}        STRING  单轮记录 JSON（TTL = CHATLOG_RETENTION_SECONDS）
    chatlog:all                    ZSET    member=trace_id, score=时间戳(ms)，全局时间线
    chatlog:index:{YYYY-MM-DD}     ZSET    当日时间线
    chatlog:user:{user_id}         ZSET    某用户的时间线
    chatlog:session:{session_id}   ZSET    某会话的时间线
    chatlog:days                   ZSET    有记录的日期
    chatlog:users                  ZSET    member=user_id, score=最近活跃时间(ms)
    chatlog:usernames              HASH    user_id -> 昵称
    chatlog:userstat:{user_id}     HASH    轮数 / Token / 错误数 / 首次与最近活跃时间

Redis 不可用时自动降级：写入内存（只保留最近 CHATLOG_MEMORY_MAX 条），
读取同样回退到内存，保证后台在本地无 Redis 时也能看到本次运行的记录。
"""
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional

from app.db import redis_client

logger = logging.getLogger("jisu.chatlog")

# 对话记录保留时间，默认 7 天
CHATLOG_RETENTION_SECONDS = int(os.getenv("CHATLOG_RETENTION_SECONDS", str(7 * 24 * 3600)))
# 一次查询最多扫描的记录条数（Redis 侧截断，避免后台翻页时全量拉取）
CHATLOG_SCAN_LIMIT = int(os.getenv("CHATLOG_SCAN_LIMIT", "2000"))
# 内存兜底最多保留的记录条数
CHATLOG_MEMORY_MAX = int(os.getenv("CHATLOG_MEMORY_MAX", "1000"))

TURN_KEY = "chatlog:turn:{trace_id}"
ALL_KEY = "chatlog:all"
DAY_KEY = "chatlog:index:{day}"
USER_KEY = "chatlog:user:{user_id}"
SESSION_KEY = "chatlog:session:{session_id}"
DAYS_KEY = "chatlog:days"
USERS_KEY = "chatlog:users"
USERNAMES_KEY = "chatlog:usernames"
USERSTAT_KEY = "chatlog:userstat:{user_id}"

# ── 内存兜底 ────────────────────────────────────────────────────
_memory: "OrderedDict[str, dict]" = OrderedDict()
_memory_users: dict[str, dict] = {}


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clip(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _memory_put(record: dict) -> None:
    """内存兜底写入（总是镜像一份，Redis 挂掉时后台仍可用）"""
    trace_id = record.get("trace_id") or f"mem-{_now_ms()}"
    _memory[trace_id] = record
    _memory.move_to_end(trace_id)
    while len(_memory) > CHATLOG_MEMORY_MAX:
        _memory.popitem(last=False)

    user_id = record.get("user_id") or "anonymous"
    stat = _memory_users.setdefault(
        user_id,
        {"user_id": user_id, "user_name": record.get("user_name") or user_id,
         "turns": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
         "errors": 0, "first_ts": record.get("ts") or _now_ms(), "last_ts": 0,
         "routes": {}},
    )
    stat["user_name"] = record.get("user_name") or stat["user_name"]
    stat["turns"] += 1
    stat["total_tokens"] += int(record.get("total_tokens") or 0)
    stat["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
    stat["completion_tokens"] += int(record.get("completion_tokens") or 0)
    if record.get("status") != "ok":
        stat["errors"] += 1
    stat["last_ts"] = max(stat["last_ts"], int(record.get("ts") or 0))
    route = record.get("route") or "unknown"
    stat["routes"][route] = stat["routes"].get(route, 0) + 1


def _memory_turns(limit: int = CHATLOG_SCAN_LIMIT) -> list[dict]:
    """内存里的记录，按时间倒序"""
    items = list(_memory.values())
    items.sort(key=lambda r: int(r.get("ts") or 0), reverse=True)
    return items[:limit]


async def record_turn(record: dict) -> None:
    """写入一轮对话记录（同时更新用户维度统计）"""
    trace_id = record.get("trace_id") or f"mem-{_now_ms()}"
    record["trace_id"] = trace_id
    record["ts"] = int(record.get("ts") or _now_ms())
    record["date"] = record.get("date") or datetime.fromtimestamp(
        record["ts"] / 1000, timezone.utc
    ).strftime("%Y-%m-%d")
    record.setdefault("user_id", "")
    record.setdefault("user_name", "")
    record.setdefault("route", "unknown")
    record.setdefault("status", "ok")

    _memory_put(record)

    user_id = record.get("user_id") or ""
    try:
        client = redis_client.get_redis()
        pipe = client.pipeline(transaction=False)
        pipe.set(TURN_KEY.format(trace_id=trace_id),
                 json.dumps(record, ensure_ascii=False), ex=CHATLOG_RETENTION_SECONDS)
        pipe.zadd(ALL_KEY, {trace_id: record["ts"]})
        pipe.expire(ALL_KEY, CHATLOG_RETENTION_SECONDS)
        pipe.zadd(DAY_KEY.format(day=record["date"]), {trace_id: record["ts"]})
        pipe.expire(DAY_KEY.format(day=record["date"]), CHATLOG_RETENTION_SECONDS)
        pipe.zadd(DAYS_KEY, {record["date"]: record["ts"]})
        pipe.expire(DAYS_KEY, CHATLOG_RETENTION_SECONDS * 4)
        # 只保留最近 CHATLOG_SCAN_LIMIT 条，防止时间线无限增长
        pipe.zremrangebyrank(ALL_KEY, 0, -(CHATLOG_SCAN_LIMIT + 1))

        session_id = record.get("session_id") or ""
        if session_id:
            key = SESSION_KEY.format(session_id=session_id)
            pipe.zadd(key, {trace_id: record["ts"]})
            pipe.expire(key, CHATLOG_RETENTION_SECONDS)
        if user_id:
            key = USER_KEY.format(user_id=user_id)
            pipe.zadd(key, {trace_id: record["ts"]})
            pipe.expire(key, CHATLOG_RETENTION_SECONDS)
            stat_key = USERSTAT_KEY.format(user_id=user_id)
            pipe.zadd(USERS_KEY, {user_id: record["ts"]})
            pipe.hset(USERNAMES_KEY, user_id, record.get("user_name") or user_id)
            pipe.hincrby(stat_key, "turns", 1)
            pipe.hincrby(stat_key, "total_tokens", int(record.get("total_tokens") or 0))
            pipe.hincrby(stat_key, "prompt_tokens", int(record.get("prompt_tokens") or 0))
            pipe.hincrby(stat_key, "completion_tokens", int(record.get("completion_tokens") or 0))
            pipe.hincrby(stat_key, "errors", 0 if record.get("status") == "ok" else 1)
            pipe.hsetnx(stat_key, "first_ts", record["ts"])
            pipe.hset(stat_key, "last_ts", record["ts"])
            pipe.hincrby(stat_key, f"route:{record['route']}", 1)
            pipe.expire(stat_key, CHATLOG_RETENTION_SECONDS)
            pipe.expire(USERNAMES_KEY, CHATLOG_RETENTION_SECONDS)
            pipe.expire(USERS_KEY, CHATLOG_RETENTION_SECONDS)
        await pipe.execute()
    except Exception as err:
        logger.warning("对话记录写入 Redis 失败（已存内存兜底）: %s", err)


def _decode(raw: Any) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        return None


async def get_turn(trace_id: str) -> Optional[dict]:
    try:
        raw = await redis_client.get_redis().get(TURN_KEY.format(trace_id=trace_id))
        record = _decode(raw)
        if record:
            return record
    except Exception as err:
        logger.warning("读取对话记录失败，回退内存: %s", err)
    return _memory.get(trace_id)


async def delete_turn(trace_id: str) -> bool:
    record = await get_turn(trace_id)
    _memory.pop(trace_id, None)
    try:
        client = redis_client.get_redis()
        pipe = client.pipeline(transaction=False)
        pipe.delete(TURN_KEY.format(trace_id=trace_id))
        for key in (
            ALL_KEY,
            DAY_KEY.format(day=(record or {}).get("date") or today()),
            USER_KEY.format(user_id=(record or {}).get("user_id") or ""),
            SESSION_KEY.format(session_id=(record or {}).get("session_id") or ""),
        ):
            pipe.zrem(key, trace_id)
        await pipe.execute()
    except Exception as err:
        logger.warning("删除对话记录失败: %s", err)
    return bool(record)


async def _candidate_ids(user_id: str = "", session_id: str = "", day: str = "") -> tuple[list[str], bool]:
    """按筛选条件取最近的时间线 trace_id

    返回 (trace_ids, redis_ok)：只有 Redis 真的不可用（redis_ok=False）时调用方才该用内存兜底，
    否则「Redis 里查不到」会被内存里的旧镜像污染（多 worker 时尤其明显）。
    """
    try:
        client = redis_client.get_redis()
        if session_id:
            key = SESSION_KEY.format(session_id=session_id)
        elif user_id:
            key = USER_KEY.format(user_id=user_id)
        elif day:
            key = DAY_KEY.format(day=day)
        else:
            key = ALL_KEY
        return list(await client.zrevrange(key, 0, CHATLOG_SCAN_LIMIT - 1)), True
    except Exception as err:
        logger.warning("读取对话索引失败，回退内存: %s", err)
    return [r["trace_id"] for r in _memory_turns()], False


async def list_turns(
    user_id: str = "",
    route: str = "",
    keyword: str = "",
    session_id: str = "",
    day: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """分页查询对话记录，支持按用户 / 入口 / 会话 / 日期 / 关键词筛选"""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 100))
    ids, redis_ok = await _candidate_ids(user_id=user_id, session_id=session_id, day=day)

    records: list[dict] = []
    if ids and redis_ok:
        try:
            client = redis_client.get_redis()
            pipe = client.pipeline(transaction=False)
            for trace_id in ids:
                pipe.get(TURN_KEY.format(trace_id=trace_id))
            raw_list = await pipe.execute()
            records = [r for r in (_decode(raw) for raw in raw_list) if r]
        except Exception as err:
            logger.warning("批量读取对话记录失败，回退内存: %s", err)
            redis_ok = False
    # 只在 Redis 不可用时才用内存兜底（否则会读到已删除的旧记录）
    if not redis_ok:
        records = _memory_turns()

    keyword = (keyword or "").strip().lower()
    filtered = []
    for record in records:
        if user_id and record.get("user_id") != user_id:
            continue
        if session_id and record.get("session_id") != session_id:
            continue
        if route and record.get("route") != route:
            continue
        if status and record.get("status") != status:
            continue
        if day and record.get("date") != day:
            continue
        if keyword:
            haystack = " ".join([
                str(record.get("question") or ""),
                str(record.get("answer") or ""),
                str(record.get("user_name") or ""),
                str(record.get("session_id") or ""),
            ]).lower()
            if keyword not in haystack:
                continue
        filtered.append(record)

    filtered.sort(key=lambda r: int(r.get("ts") or 0), reverse=True)
    total = len(filtered)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "scanned": len(records),
        "truncated": len(records) >= CHATLOG_SCAN_LIMIT,
        "items": filtered[start:start + page_size],
    }


async def list_users() -> list[dict]:
    """用户维度汇总（对话轮数 / Token / 最近活跃），按最近活跃倒序"""
    users: dict[str, dict] = {}
    redis_ok = True
    try:
        client = redis_client.get_redis()
        pairs = await client.zrevrange(USERS_KEY, 0, -1, withscores=True)
        if pairs:
            pipe = client.pipeline(transaction=False)
            for user_id, _ in pairs:
                pipe.hgetall(USERSTAT_KEY.format(user_id=user_id))
                pipe.hget(USERNAMES_KEY, user_id)
            values = await pipe.execute()
            for index, (user_id, score) in enumerate(pairs):
                raw = _to_int_map(values[index * 2] or {})
                routes = {
                    key.split(":", 1)[1]: value
                    for key, value in raw.items() if key.startswith("route:")
                }
                users[user_id] = {
                    "user_id": user_id,
                    "user_name": values[index * 2 + 1] or user_id,
                    "turns": raw.get("turns", 0),
                    "total_tokens": raw.get("total_tokens", 0),
                    "prompt_tokens": raw.get("prompt_tokens", 0),
                    "completion_tokens": raw.get("completion_tokens", 0),
                    "errors": raw.get("errors", 0),
                    "first_ts": raw.get("first_ts", 0),
                    "last_ts": raw.get("last_ts") or int(score),
                    "routes": routes,
                }
    except Exception as err:
        logger.warning("读取用户汇总失败，回退内存: %s", err)
        redis_ok = False

    # 只在 Redis 不可用时才合并内存镜像，否则清空后的用户会从旧镜像里复活
    for user_id, stat in (_memory_users.items() if not redis_ok else ()):
        current = users.get(user_id)
        if current is None:
            users[user_id] = {
                "user_id": user_id,
                "user_name": stat.get("user_name") or user_id,
                "turns": stat.get("turns", 0),
                "total_tokens": stat.get("total_tokens", 0),
                "prompt_tokens": stat.get("prompt_tokens", 0),
                "completion_tokens": stat.get("completion_tokens", 0),
                "errors": stat.get("errors", 0),
                "first_ts": stat.get("first_ts", 0),
                "last_ts": stat.get("last_ts", 0),
                "routes": stat.get("routes", {}),
                "memory_only": True,
            }

    result = list(users.values())
    result.sort(key=lambda u: int(u.get("last_ts") or 0), reverse=True)
    return result


async def purge_user(user_id: str) -> dict:
    """清空某个用户的全部对话记录（记录本体 + 各索引 + 用户汇总）"""
    user_id = (user_id or "").strip()
    if not user_id:
        return {"user_id": user_id, "deleted_turns": 0, "redis_ok": False}

    ids, _redis_ok = await _candidate_ids(user_id=user_id)
    deleted = 0
    for trace_id in ids:
        if await delete_turn(trace_id):
            deleted += 1

    redis_ok = True
    try:
        client = redis_client.get_redis()
        pipe = client.pipeline(transaction=False)
        pipe.delete(USERSTAT_KEY.format(user_id=user_id))
        pipe.delete(USER_KEY.format(user_id=user_id))
        pipe.hdel(USERNAMES_KEY, user_id)
        pipe.zrem(USERS_KEY, user_id)
        await pipe.execute()
    except Exception as err:
        redis_ok = False
        logger.warning("清空用户汇总失败: %s", err)

    # 内存兜底一并清掉
    for key, record in list(_memory.items()):
        if record.get("user_id") == user_id:
            _memory.pop(key, None)
    _memory_users.pop(user_id, None)

    return {"user_id": user_id, "deleted_turns": deleted, "redis_ok": redis_ok}


async def purge_all() -> dict:
    """清空全部对话记录（含所有索引与用户汇总）"""
    deleted = 0
    redis_ok = True
    try:
        client = redis_client.get_redis()
        for pattern in ("chatlog:turn:*", "chatlog:index:*", "chatlog:user:*",
                        "chatlog:session:*", "chatlog:userstat:*"):
            keys = await client.keys(pattern)
            for start in range(0, len(keys), 500):
                batch = keys[start:start + 500]
                if batch:
                    deleted += int(await client.delete(*batch))
        await client.delete(ALL_KEY, DAYS_KEY, USERS_KEY, USERNAMES_KEY)
    except Exception as err:
        redis_ok = False
        logger.warning("清空全部对话记录失败: %s", err)

    _memory.clear()
    _memory_users.clear()
    return {"deleted_keys": deleted, "redis_ok": redis_ok}


def _to_int_map(raw: dict) -> dict:
    out: dict = {}
    for key, value in (raw or {}).items():
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            out[key] = value
    return out


async def list_retrievals(limit: int = 50, user_id: str = "", keyword: str = "") -> list[dict]:
    """最近的「知识库检索 + 重排」明细（跨用户）"""
    limit = max(1, min(int(limit), 200))
    ids, redis_ok = await _candidate_ids(user_id=user_id)
    records: list[dict] = []
    if ids and redis_ok:
        try:
            client = redis_client.get_redis()
            pipe = client.pipeline(transaction=False)
            for trace_id in ids:
                pipe.get(TURN_KEY.format(trace_id=trace_id))
            records = [r for r in (_decode(raw) for raw in await pipe.execute()) if r]
        except Exception as err:
            logger.warning("读取检索明细失败，回退内存: %s", err)
            redis_ok = False
    # 同样只在 Redis 不可用时才用内存兜底
    if not redis_ok:
        records = _memory_turns()

    keyword = (keyword or "").strip().lower()
    result = []
    for record in sorted(records, key=lambda r: int(r.get("ts") or 0), reverse=True):
        retrievals = record.get("retrievals") or []
        if not retrievals:
            continue
        if user_id and record.get("user_id") != user_id:
            continue
        if keyword:
            haystack = " ".join(
                [str(record.get("question") or "")]
                + [str(item.get("query") or "") for item in retrievals]
            ).lower()
            if keyword not in haystack:
                continue
        result.append({
            "trace_id": record.get("trace_id"),
            "ts": record.get("ts"),
            "date": record.get("date"),
            "route": record.get("route"),
            "user_id": record.get("user_id"),
            "user_name": record.get("user_name"),
            "session_id": record.get("session_id"),
            "question": record.get("question"),
            "status": record.get("status"),
            "retrievals": retrievals,
        })
        if len(result) >= limit:
            break
    return result


def memory_days() -> list[str]:
    """内存兜底里有记录的日期（Redis 不可用时给后台一个可用日期列表）"""
    return sorted({r.get("date") for r in _memory_turns() if r.get("date")}, reverse=True)
