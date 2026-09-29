import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

# 统一日志配置：usage 统计等结构化日志会打到 stdout
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.postgres import close_pool, init_pool
from app.observability import trace_store
from app.routers import (
    admin,
    agent,
    chat,
    gate,
    graph,
    identity as identity_router,
    observability,
    rag,
    trace,
)
from app.security.middleware import IdentityMiddleware

logger = logging.getLogger("jisu.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动/退出钩子

    PostgreSQL 连接池只服务全链路追踪：连不上就静默降级（追踪不落库），
    **绝不拦住服务启动** —— 追踪是运维工具，不是业务依赖。
    """
    pool = await init_pool()
    if pool is not None:
        await trace_store.init_schema(pool)
    else:
        logger.warning("PostgreSQL 未就绪：全链路追踪不可用，其余功能正常")
    yield
    await close_pool()


app = FastAPI(title="极速购 AI 客服系统", lifespan=lifespan)

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
# 全链路追踪：后台「链路追踪」页签用，鉴权与其它后台接口一致（run 详情的深链靠 run_id）
app.include_router(trace.router, prefix="/api/admin/trace")


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
