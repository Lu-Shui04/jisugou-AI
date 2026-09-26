"""身份接口：登录（选择用户）/ 查看当前身份 / 权限事件

POST /api/identity/login   选择一个用户身份 → 服务端签发 HMAC 令牌（前端左上角切换用户走这里）
GET  /api/identity/users   可选的用户列表（U-100 ~ U-104）
GET  /api/identity/me      当前令牌对应的身份（前端启动时校验，令牌过期会自动重登）
POST /api/identity/logout  退出（令牌是无状态的，客户端丢弃即可）
GET  /api/identity/incidents  最近的权限事件（越权尝试留痕，演示/排查用）

安全边界：登录接口是**唯一**能决定"我是谁"的地方，而且它签发的令牌带 HMAC 签名 ——
客户端可以选身份（演示环境），但选完就改不了了。之后所有订单接口都只认令牌，
请求体里的 user_id 改破天也没用（见 app/security/access.py 与 tools/order_tools.py）。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.security import identity

router = APIRouter()


class LoginRequest(BaseModel):
    userId: str


@router.get("/users")
async def users():
    principal = identity.current_principal()
    return {
        "users": identity.list_users(),
        "default_user_id": identity.default_user_id(),
        "current": principal.as_dict(),
    }


@router.post("/login")
async def login(req: LoginRequest):
    """选择身份 → 签发令牌（演示环境用"选用户"代替账号密码登录）"""
    user = identity.get_user(req.userId or "")
    if user is None:
        return JSONResponse(
            status_code=400,
            content={"error": f"用户 {req.userId} 不存在",
                     "users": identity.known_user_ids()},
        )
    token = identity.issue_token(user.user_id)
    principal, _reason = identity.verify_token(token)
    identity.record_incident("identity_login", actor=user.user_id, scene="login")
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": identity.TOKEN_TTL_SECONDS,
        "user": user.as_dict(),
        "principal": principal.as_dict() if principal else {},
    }


@router.get("/me")
async def me():
    principal = identity.current_principal()
    return {
        "authenticated": principal.authenticated,
        "principal": principal.as_dict(),
        "user": identity.get_user(principal.user_id).as_dict() if principal.authenticated else None,
    }


@router.post("/logout")
async def logout():
    # 令牌是无状态的：服务端不维护会话表，客户端丢掉令牌即退出
    principal = identity.current_principal()
    return {"ok": True, "logged_out": principal.user_id or "anonymous"}


@router.get("/incidents")
async def incidents(limit: int = 50):
    return {
        "counts": identity.incident_counts(),
        "items": identity.recent_incidents(limit=limit),
    }
