import logging
import os

from dotenv import load_dotenv

load_dotenv()

# 统一日志配置：usage 统计等结构化日志会打到 stdout
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import (
    admin,
    agent,
    chat,
    gate,
    graph,
    identity as identity_router,
    observability,
    rag,
)
from app.security.middleware import IdentityMiddleware

app = FastAPI(title="极速购 AI 客服系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# 身份中间件放在最外层：请求一进来就把令牌换成"我是谁"，
# 后面所有工具 / 节点 / 会话缓存都从上下文里取（请求体里的 user_id 不参与鉴权）
app.add_middleware(IdentityMiddleware)

# 开屏人机验证（滑块）：下发 challenge / 校验并签发 Token
app.include_router(gate.router, prefix="/api/gate")
# 身份：登录（选择用户）/ 当前身份 / 权限事件
app.include_router(identity_router.router, prefix="/api/identity")
app.include_router(chat.router, prefix="/api/chat")
app.include_router(agent.router, prefix="/api/agent")
app.include_router(rag.router, prefix="/api/rag")
app.include_router(graph.router, prefix="/api/graph")
app.include_router(observability.router, prefix="/api/observability")
# 管理员后台：左上角入口登录后可见（账号密码见 .env 的 ADMIN_USERNAME / ADMIN_PASSWORD）
app.include_router(admin.router, prefix="/api/admin")


@app.get("/")
async def root():
    return {
        "service": "极速购 AI 客服系统",
        "version": "1.2.0",
        "routes": {
            "chat": "POST /api/chat/stream",
            "agent": "POST /api/agent/stream",
            "rag": "POST /api/rag/query",
            "graph": "POST /api/graph/stream",
            "usage": "GET /api/observability/usage",
            "session": "GET /api/observability/session/{session_id}",
            "identity_users": "GET /api/identity/users",
            "identity_login": "POST /api/identity/login",
            "identity_me": "GET /api/identity/me",
            "identity_incidents": "GET /api/identity/incidents",
            "admin_login": "POST /api/admin/login",
            "admin_overview": "GET /api/admin/overview",
            "admin_conversations": "GET /api/admin/conversations",
            "admin_retrievals": "GET /api/admin/retrievals",
            "admin_system": "GET /api/admin/system",
        },
        "session_cache": "redis: session:{session_id}, ttl 1800s",
    }
