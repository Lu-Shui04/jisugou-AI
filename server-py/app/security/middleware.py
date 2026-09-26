"""身份中间件：请求进来的第一件事，就是把"我是谁"换成服务端认定的身份

纯 ASGI 中间件（不是 BaseHTTPMiddleware）：不开子任务、不缓冲响应体，
SSE 流式输出不受影响，ContextVar 也能顺着同一条调用链传到工具里。
"""
from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.security import identity


class IdentityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        token = identity.bearer_token(scope.get("headers") or [])
        principal, reason = identity.verify_token(token)
        if principal is None:
            principal = identity.Principal(authenticated=False, reason=reason)
            # 没带令牌是正常的（没登录、查知识库）；带了但验不过才是可疑的
            if token and reason != "no_token":
                identity.record_incident("identity_rejected", reason=reason, path=path)

        marker = identity.set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.reset_principal(marker)
