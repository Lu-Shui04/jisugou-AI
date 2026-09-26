"""会话解析：session_id 与历史加载

优先级：Redis 会话缓存（key = session_id） > 请求体里的 history。
Redis 没有数据（首次请求 / 已过期 / 服务不可用）时才用前端传来的历史兜底。
"""
import uuid

from app.db.redis_client import get_history
from app.db.redis_client import SESSION_TTL_SECONDS  # noqa: F401  (供路由推送 TTL)


async def resolve_session(session_id: str | None, request_history: list, current_message: str) -> tuple[str, list[dict]]:
    """返回 (session_id, 历史消息列表)

    - session_id 为空时生成一个新的
    - Redis 命中则用服务端历史（前端不用再传全量历史）
    - 未命中则用请求体历史，并去掉与当前消息重复的最后一条用户消息
    """
    session_id = (session_id or "").strip() or uuid.uuid4().hex

    history = await get_history(session_id)
    if history:
        return session_id, history

    fallback = [
        {"role": m.role if hasattr(m, "role") else m.get("role"),
         "content": m.content if hasattr(m, "content") else m.get("content")}
        for m in (request_history or [])
    ]
    if fallback and fallback[-1].get("role") == "user" and fallback[-1].get("content") == current_message:
        fallback = fallback[:-1]
    return session_id, fallback
