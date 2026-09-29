"""订单数据权限（行级权限）：每一次取数据都问一句"这条数据是不是他的"

一句话规则：**只有本人能看本人的订单**。
- 请求里带来的 userId 只是"用户声称要查谁"，必须与令牌里的真实用户 ID 完全一致才是本人；
- 订单归属以数据里的 userId 为准，任何请求参数都不能改写它；
- 拿不到身份（匿名 / 令牌过期 / 验签失败）一律拒绝，不给数据（fail closed）。

覆盖三条取数路径，缺一条就是越权漏洞：
    getOrderInfo      按订单号查详情   → 校验订单归属
    getUserOrders     按用户 ID 查列表 → 忽略传入 ID，只查本人
    getLogisticsInfo  按快递单号查轨迹 → 快递单号反查订单归属（否则绕开订单号就能看别人物流）
另外还有一条隐蔽路径：会话缓存 / 前端回传的 history 里可能带着别人的订单号，
被模型当成"事实"复述出来 —— scope_history() 负责在入口处把这类消息裁掉。
"""
from __future__ import annotations

from app.data.mock import orders
from app.security import identity
from app.utils.ids import normalize_id, referenced_ids
from app.utils.messages import message_content


# ── 基础判断 ─────────────────────────────────────────────────────
def find_order(order_id: str) -> dict | None:
    return orders.get(normalize_id(order_id))


def find_order_by_tracking(tracking_no: str) -> dict | None:
    """快递单号 → 订单（物流数据挂在订单上，权限也必须跟着订单走）"""
    target = normalize_id(tracking_no)
    for order in orders.values():
        if normalize_id(order.get("trackingNo") or "") == target:
            return order
    return None


def orders_of(user_id: str) -> list[dict]:
    target = normalize_id(user_id)
    return [order for order in orders.values() if order["userId"] == target]


def owns_user(principal: identity.Principal, user_id: str) -> bool:
    return bool(principal.authenticated) and normalize_id(user_id) == principal.user_id


def owns_order(principal: identity.Principal, order_id: str) -> bool:
    order = find_order(order_id)
    return bool(order) and owns_user(principal, order.get("userId") or "")


def owns_tracking(principal: identity.Principal, tracking_no: str) -> bool:
    order = find_order_by_tracking(tracking_no)
    return bool(order) and owns_user(principal, order.get("userId") or "")


# ── 拒绝话术（给模型看的 JSON，模型据此如实转达）──────────────────
def deny_unauthenticated(principal: identity.Principal) -> dict:
    return {
        "error": "未登录，无法查询订单数据",
        "code": "UNAUTHENTICATED",
        "reason": principal.reason,
        "hint": principal.denial_message() + " 请如实告知用户需要先选择身份，不要编造任何订单数据。",
    }


def deny_foreign_user(principal: identity.Principal, requested: str) -> dict:
    identity.record_incident("order_scope_denied", actor=principal.user_id,
                             target=normalize_id(requested), scene="getUserOrders")
    return {
        "error": f"无权查看用户 {normalize_id(requested)} 的订单：当前登录用户是 {principal.user_id}",
        "code": "FORBIDDEN",
        "hint": "系统只允许查询本人订单。请如实告知用户无权查看他人订单，"
                "不要换用户 ID 重试、不要猜测订单内容。",
    }


def deny_foreign_order(principal: identity.Principal, order_id: str) -> dict:
    identity.record_incident("order_scope_denied", actor=principal.user_id,
                             target=normalize_id(order_id), scene="getOrderInfo")
    return {
        "error": f"无权查看订单 {normalize_id(order_id)}：它不属于当前登录用户 {principal.user_id}",
        "code": "FORBIDDEN",
        "hint": "系统只允许查询本人名下的订单。请如实告知用户无权查看，"
                "不要换订单号重试、不要编造订单内容。",
    }


def deny_foreign_tracking(principal: identity.Principal, tracking_no: str) -> dict:
    identity.record_incident("order_scope_denied", actor=principal.user_id,
                             target=normalize_id(tracking_no), scene="getLogisticsInfo")
    return {
        "error": f"无权查看快递 {normalize_id(tracking_no)} 的物流：该包裹不属于当前登录用户 {principal.user_id}",
        "code": "FORBIDDEN",
        "hint": "系统只允许查询本人包裹的物流。请如实告知用户无权查看，不要编造物流信息。",
    }


# ── 会话历史裁剪：别人订单号不能顺着 history 溜进上下文 ─────────────
def _message_role(message) -> str:
    if isinstance(message, dict):
        return str(message.get("role") or "")
    role = str(getattr(message, "role", "") or "")
    if role:
        return role
    return {"ai": "assistant", "human": "user", "system": "system",
            "tool": "tool"}.get(str(getattr(message, "type", "") or ""), "")


def foreign_ids(text: str, principal: identity.Principal) -> dict:
    """这段文本里引用了哪些"不属于当前用户"的 ID"""
    found = referenced_ids(text or "")
    foreign_orders = [oid for oid in found["orders"]
                      if find_order(oid) and not owns_order(principal, oid)]
    foreign_trackings = [tno for tno in found["trackings"]
                         if find_order_by_tracking(tno) and not owns_tracking(principal, tno)]
    foreign_users = [uid for uid in found["users"]
                     if identity.get_user(uid) and not owns_user(principal, uid)]
    return {"orders": foreign_orders, "trackings": foreign_trackings, "users": foreign_users}


def scope_history(history, principal: identity.Principal) -> tuple[list, list[dict]]:
    """把会话历史里引用他人数据的消息裁掉，返回 (干净历史, 被裁掉的记录)

    场景：用户 A 查完订单，会话缓存 / 前端 localStorage 里留着 A 的订单号；
    换用户 B 进来（或 B 伪造一份 history），模型会把 A 的订单当成"已核实的事实"复述出去。
    入口处裁掉，比指望模型自觉可靠。
    """
    if not history:
        return [], []
    if not principal.authenticated:
        # 匿名（没选身份 / 令牌过期）：谁都无权，带 ID 的历史一概不留
        kept, dropped = [], []
        for message in history:
            content = message_content(message)
            hits = foreign_ids(content, identity.ANONYMOUS)
            if any(hits.values()) or referenced_ids(content)["orders"]:
                dropped.append({"role": _message_role(message), "hits": hits})
            else:
                kept.append(message)
        if dropped:
            identity.record_incident("history_scope_denied", actor="anonymous",
                                     scene="scope_history", count=len(dropped))
        return kept, dropped

    kept, dropped = [], []
    for message in history:
        hits = foreign_ids(message_content(message), principal)
        if any(hits.values()):
            dropped.append({"role": _message_role(message), "hits": hits})
            continue
        kept.append(message)
    if dropped:
        identity.record_incident("history_scope_denied", actor=principal.user_id,
                                 scene="scope_history", count=len(dropped),
                                 samples=dropped[:3])
    return kept, dropped
