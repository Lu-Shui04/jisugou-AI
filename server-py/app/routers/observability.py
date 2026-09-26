"""可观测接口：Token 用量统计 + 会话缓存查看

GET    /api/observability/usage               当日/累计 Token 消耗
GET    /api/observability/session/{session_id} 会话缓存内容与剩余 TTL
DELETE /api/observability/session/{session_id} 清空会话缓存

会话缓存里装着用户问过的订单，所以这两个会话接口**只对本人生效**：
不是你的会话，读不到（当成空会话）也删不掉（越权尝试会记一条权限事件）。
"""
from fastapi import APIRouter, Query

from app.db.redis_client import (
    SESSION_TTL_SECONDS,
    clear_session,
    get_history,
    session_ttl,
)
from app.observability.usage import get_usage_stats
from app.security import identity

router = APIRouter()


@router.get("/usage")
async def usage_stats(date: str | None = Query(default=None, description="UTC 日期，格式 YYYY-MM-DD")):
    return await get_usage_stats(date)


@router.get("/session/{session_id}")
async def session_detail(session_id: str):
    owner = identity.current_principal().user_id
    history = await get_history(session_id, owner=owner)
    return {
        "session_id": session_id,
        "key": f"session:{session_id}",
        "owner": owner or "anonymous",
        "ttl": await session_ttl(session_id),
        "ttl_limit": SESSION_TTL_SECONDS,
        "messages": history,
    }


@router.delete("/session/{session_id}")
async def session_clear(session_id: str):
    owner = identity.current_principal().user_id
    cleared = await clear_session(session_id, owner=owner)
    return {"session_id": session_id, "cleared": cleared}
