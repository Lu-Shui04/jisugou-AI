"""会话解析：session_id 与历史加载

优先级：Redis 会话缓存（key = session_id，value 里带 owner） > 请求体里的 history。
Redis 没有数据（首次请求 / 已过期 / 服务不可用）时才用前端传来的历史兜底。

两条权限约束（这次补的）：
    1. 缓存里的会话必须属于当前登录用户，否则当"没有会话"（见 redis_client.get_history）；
    2. 请求体里回传的 history 同样要过一遍数据权限：它可能引用别人的订单号，
       而模型会把历史里的内容当成"已经核实过的事实"复述出去 —— 那是绕开工具的
       一条横向越权路径，必须在入口裁掉（access.scope_history）。
"""
import uuid

from app.db.redis_client import get_history
from app.security import access, identity


async def resolve_session(session_id: str | None, request_history: list, current_message: str,
                          principal: identity.Principal | None = None) -> tuple[str, list[dict]]:
    """返回 (session_id, 历史消息列表)

    - session_id 为空时生成一个新的
    - Redis 命中则用服务端历史（前端不用再传全量历史）
    - 未命中则用请求体历史，并去掉与当前消息重复的最后一条用户消息
    """
    session_id = (session_id or "").strip() or uuid.uuid4().hex
    principal = principal or identity.current_principal()
    owner = principal.user_id

    history = await get_history(session_id, owner=owner)
    if history:
        return session_id, history

    fallback = [
        {"role": m.role if hasattr(m, "role") else m.get("role"),
         "content": m.content if hasattr(m, "content") else m.get("content")}
        for m in (request_history or [])
    ]
    if fallback and fallback[-1].get("role") == "user" and fallback[-1].get("content") == current_message:
        fallback = fallback[:-1]
    # 前端回传的历史按当前身份裁剪（别人的订单号在这里就被丢掉）
    fallback, dropped = access.scope_history(fallback, principal)
    if dropped:
        print(f"[Session] 历史里含他人数据，已裁掉 {len(dropped)} 条（用户 {owner or 'anonymous'}）")
    return session_id, fallback
