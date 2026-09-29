"""管理员后台接口

登录后拿到访问令牌（放在请求头 X-Admin-Token 或 Authorization: Bearer），
默认账号密码 admin / 123456，可用 .env 的 ADMIN_USERNAME / ADMIN_PASSWORD 覆盖。

    POST   /api/admin/login                      账号密码登录，返回访问令牌
    POST   /api/admin/logout                     退出登录（令牌失效）
    GET    /api/admin/me                         当前登录信息
    GET    /api/admin/overview                   总览看板（今日/累计 Token、活跃用户、趋势）
    GET    /api/admin/usage                      Token 用量明细（按入口 / 模型 / 用户 / 日期）
    GET    /api/admin/users                      用户列表（对话轮数 / Token / 最近活跃）
    GET    /api/admin/conversations              聊天记录列表（用户 / 入口 / 关键词 / 日期筛选）
    GET    /api/admin/conversations/{trace_id}   单轮详情（含检索与重排明细、工具调用）
    DELETE /api/admin/conversations/{trace_id}   删除单轮记录
    DELETE /api/admin/users/{user_id}/conversations  清空该用户的全部对话记录
    DELETE /api/admin/users/{user_id}/usage          清空该用户的 Token 统计
    DELETE /api/admin/conversations              清空全部对话记录
    DELETE /api/admin/usage                      清空全部 Token 统计
    GET    /api/admin/retrievals                 最近的知识库检索 + 重排明细
    GET    /api/admin/sessions/{session_id}      会话缓存内容与剩余 TTL
    DELETE /api/admin/sessions/{session_id}      清空会话缓存
    GET    /api/admin/security                   提示词安全防护（配置 / 统计 / 拦截事件）
    POST   /api/admin/security/check             规则自测：拿一段文字过一遍防护链路
    DELETE /api/admin/security/events            清空拦截事件
    DELETE /api/admin/security/stats             清空安全统计
    GET    /api/admin/system                     系统状态（Redis / 数据库 / 模型 / 知识库 / 配置）
    GET    /api/admin/export/conversations.csv   导出聊天记录 CSV
"""
import asyncio
import csv
import hmac
import io
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db import redis_client
from app.db.redis_client import (
    SESSION_TTL_SECONDS,
    clear_session,
    get_history,
    session_ttl,
)
from app.observability import chatlog
from app.security import guard as security_guard
from app.security import store as security_store
from app.observability.usage import (
    USAGE_RETENTION_SECONDS,
    get_usage_overview,
    get_usage_stats,
    purge_all_usage,
    purge_user_usage,
)

logger = logging.getLogger("jisu.admin")

router = APIRouter()

# ── 登录配置 ────────────────────────────────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123456")
# 免登录开关：默认 false（打开后台直接进，不用账号密码）
# 需要恢复登录时，在 .env 里设 ADMIN_AUTH_ENABLED=true
ADMIN_AUTH_ENABLED = os.getenv("ADMIN_AUTH_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
ADMIN_TOKEN_TTL = int(os.getenv("ADMIN_TOKEN_TTL", str(8 * 3600)))
# 登录失败次数限制：LOGIN_WINDOW_SECONDS 内最多 LOGIN_MAX_FAILURES 次
LOGIN_MAX_FAILURES = int(os.getenv("ADMIN_LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "300"))

TOKEN_KEY = "admin:token:{token}"

# 内存兜底：Redis 不可用时令牌仍可用（进程重启即失效）
_tokens: dict[str, dict] = {}
_login_failures: dict[str, list[float]] = {}


def _token_payload(token: str) -> dict:
    return {"token": token, "username": ADMIN_USERNAME, "login_at": int(time.time())}


async def _save_token(token: str) -> dict:
    payload = _token_payload(token)
    _tokens[token] = {**payload, "expires_at": payload["login_at"] + ADMIN_TOKEN_TTL}
    try:
        await redis_client.get_redis().set(
            TOKEN_KEY.format(token=token), json.dumps(payload, ensure_ascii=False), ex=ADMIN_TOKEN_TTL
        )
    except Exception as err:
        logger.warning("管理员令牌写入 Redis 失败（使用内存令牌）: %s", err)
    return payload


async def _load_token(token: str) -> Optional[dict]:
    cached = _tokens.get(token)
    if cached and cached.get("expires_at", 0) > time.time():
        return cached
    try:
        raw = await redis_client.get_redis().get(TOKEN_KEY.format(token=token))
        if raw:
            payload = json.loads(raw)
            _tokens[token] = {**payload, "expires_at": int(time.time()) + ADMIN_TOKEN_TTL}
            return payload
    except Exception as err:
        logger.warning("读取管理员令牌失败: %s", err)
    return cached


async def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    """鉴权依赖：默认免登录直接放行；开启 ADMIN_AUTH_ENABLED 后才校验令牌"""
    if not ADMIN_AUTH_ENABLED:
        return {"username": ADMIN_USERNAME, "role": "admin", "login_at": None, "anonymous": True}

    token = (x_admin_token or "").strip()
    if not token and authorization:
        token = authorization.split()[-1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请先登录管理员后台")
    info = await _load_token(token)
    if not info:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return info


def _check_login_limit(ip: str) -> None:
    now = time.time()
    attempts = [ts for ts in _login_failures.get(ip, []) if now - ts < LOGIN_WINDOW_SECONDS]
    _login_failures[ip] = attempts
    if len(attempts) >= LOGIN_MAX_FAILURES:
        wait = int(LOGIN_WINDOW_SECONDS - (now - attempts[0]))
        raise HTTPException(status_code=429, detail=f"登录失败次数过多，请 {max(wait, 1)} 秒后再试")


class LoginPayload(BaseModel):
    username: str
    password: str


# ── 登录 / 登出 ─────────────────────────────────────────────────
@router.post("/login")
async def login(payload: LoginPayload, request: Request):
    ip = request.client.host if request.client else "unknown"
    _check_login_limit(ip)

    ok_user = hmac.compare_digest(payload.username.strip(), ADMIN_USERNAME)
    ok_password = hmac.compare_digest(payload.password, ADMIN_PASSWORD)
    if not (ok_user and ok_password):
        _login_failures.setdefault(ip, []).append(time.time())
        logger.warning("管理员登录失败 ip=%s username=%s", ip, payload.username)
        raise HTTPException(status_code=401, detail="账号或密码错误")

    _login_failures.pop(ip, None)
    token = secrets.token_urlsafe(32)
    info = await _save_token(token)
    logger.info("管理员登录成功 ip=%s", ip)
    return {**info, "expires_in": ADMIN_TOKEN_TTL, "role": "admin"}


@router.post("/logout")
async def logout(admin: dict = Depends(require_admin)):
    token = admin.get("token", "")
    _tokens.pop(token, None)
    try:
        await redis_client.get_redis().delete(TOKEN_KEY.format(token=token))
    except Exception as err:
        logger.warning("删除管理员令牌失败: %s", err)
    return {"logged_out": True}


@router.get("/me")
async def me(admin: dict = Depends(require_admin)):
    return {
        "username": admin.get("username", ADMIN_USERNAME),
        "login_at": admin.get("login_at"),
        "role": "admin",
    }


# ── 看板与统计 ──────────────────────────────────────────────────
@router.get("/overview")
async def overview(
    days: int = Query(default=7, ge=1, le=30, description="趋势天数"),
    admin: dict = Depends(require_admin),
):
    usage = await get_usage_overview(days)
    users = await chatlog.list_users()
    recent = await chatlog.list_turns(page=1, page_size=10)
    today = usage["today"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "usage": usage,
        "users": {
            "total": len(users),
            "active_today": today.get("active_users", 0),
            "top": users[:10],
        },
        "recent_turns": recent["items"],
        "routes": usage["routes"],
        "models": usage["models"],
    }


@router.get("/usage")
async def usage_detail(
    date: Optional[str] = Query(default=None, description="UTC 日期 YYYY-MM-DD，默认今天"),
    days: int = Query(default=7, ge=1, le=30),
    admin: dict = Depends(require_admin),
):
    overview_data = await get_usage_overview(days)
    stats = await get_usage_stats(date)
    return {
        "date": stats["date"],
        "today": stats["today"],
        "total": stats["total"],
        "routes": stats["routes"],
        "models": stats["models"],
        "users": stats["users"],
        "top_users": overview_data["top_users"],
        "trend": overview_data["trend"],
        "days": stats.get("days", []),
        "redis_available": "error" not in stats,
        "retention_seconds": USAGE_RETENTION_SECONDS,
    }


@router.get("/users")
async def users_list(admin: dict = Depends(require_admin)):
    items = await chatlog.list_users()
    return {"total": len(items), "items": items}


@router.get("/conversations")
async def conversations(
    user_id: str = Query(default="", description="按用户筛选"),
    session_id: str = Query(default="", description="按会话筛选"),
    route: str = Query(default="", description="入口：chat / agent / rag / graph"),
    keyword: str = Query(default="", description="问题或回答关键词"),
    day: str = Query(default="", description="UTC 日期 YYYY-MM-DD"),
    status: str = Query(default="", description="ok / error"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    admin: dict = Depends(require_admin),
):
    return await chatlog.list_turns(
        user_id=user_id, session_id=session_id, route=route,
        keyword=keyword, day=day, status=status,
        page=page, page_size=page_size,
    )


@router.get("/conversations/{trace_id}")
async def conversation_detail(trace_id: str, admin: dict = Depends(require_admin)):
    record = await chatlog.get_turn(trace_id)
    if not record:
        raise HTTPException(status_code=404, detail="记录不存在或已过期")
    return record


@router.delete("/conversations/{trace_id}")
async def conversation_delete(trace_id: str, admin: dict = Depends(require_admin)):
    return {"trace_id": trace_id, "deleted": await chatlog.delete_turn(trace_id)}


@router.delete("/users/{user_id}/conversations")
async def clear_user_conversations(user_id: str, admin: dict = Depends(require_admin)):
    """清空某个用户的全部对话记录"""
    return await chatlog.purge_user(user_id)


@router.delete("/users/{user_id}/usage")
async def clear_user_usage(user_id: str, admin: dict = Depends(require_admin)):
    """清空某个用户的 Token 统计条目（总计 / 按天等历史累计无法按用户回滚）"""
    return await purge_user_usage(user_id)


@router.delete("/conversations")
async def clear_all_conversations(admin: dict = Depends(require_admin)):
    """清空全部对话记录"""
    return await chatlog.purge_all()


@router.delete("/usage")
async def clear_all_usage(admin: dict = Depends(require_admin)):
    """清空全部 Token 用量统计"""
    return await purge_all_usage()


@router.get("/retrievals")
async def retrievals(
    limit: int = Query(default=30, ge=1, le=200),
    user_id: str = Query(default=""),
    keyword: str = Query(default=""),
    admin: dict = Depends(require_admin),
):
    """最近的知识库检索 + 重排（相似度打分、阈值过滤）明细"""
    items = await chatlog.list_retrievals(limit=limit, user_id=user_id, keyword=keyword)
    return {"total": len(items), "items": items}


# ── 会话缓存 ────────────────────────────────────────────────────
@router.get("/sessions")
async def sessions_list(
    limit: int = Query(default=50, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    """列出 Redis 里现有的会话，供后台下拉选择（不用手输 session_id）

    顺带把每个会话最近一轮的用户名带出来，方便辨认是谁的会话。
    """
    items: list[dict] = []
    try:
        client = redis_client.get_redis()
        keys: list[str] = []
        cursor = 0
        while True:
            cursor, batch = await client.scan(cursor=cursor, match="session:*", count=200)
            keys.extend(batch)
            if cursor == 0 or len(keys) >= limit * 4:
                break
        keys = keys[: limit * 4]

        if keys:
            pipe = client.pipeline(transaction=False)
            for key in keys:
                pipe.get(key)
                pipe.ttl(key)
            values = await pipe.execute()

            # 会话 → 用户：从最近的对话记录里一次性建映射
            owners: dict[str, dict] = {}
            try:
                recent = await chatlog.list_turns(page=1, page_size=200)
                for turn in recent.get("items", []):
                    sid = turn.get("session_id")
                    if sid and sid not in owners:
                        owners[sid] = {
                            "user_name": turn.get("user_name") or "",
                            "user_id": turn.get("user_id") or "",
                        }
            except Exception as err:
                logger.warning("读取会话归属失败: %s", err)

            for index, key in enumerate(keys):
                raw = values[index * 2] or "[]"
                try:
                    messages = json.loads(raw)
                except (TypeError, ValueError):
                    messages = []
                sid = key.split(":", 1)[1]
                owner = owners.get(sid, {})
                items.append({
                    "session_id": sid,
                    "key": key,
                    "ttl": int(values[index * 2 + 1]),
                    "messages": len(messages),
                    "chars": len(raw),
                    "last_message": (messages[-1].get("content", "")[:60] if messages else ""),
                    "user_name": owner.get("user_name", ""),
                    "user_id": owner.get("user_id", ""),
                })
    except Exception as err:
        logger.warning("列出会话失败: %s", err)

    # 快过期的排前面（更可能是用户正在用的会话）
    items.sort(key=lambda item: item["ttl"])
    return {"total": len(items), "items": items[:limit]}


@router.get("/sessions/{session_id}")
async def session_detail(session_id: str, admin: dict = Depends(require_admin)):
    return {
        "session_id": session_id,
        "key": f"session:{session_id}",
        "ttl": await session_ttl(session_id),
        "ttl_limit": SESSION_TTL_SECONDS,
        "messages": await get_history(session_id),
    }


@router.delete("/sessions/{session_id}")
async def session_clear(session_id: str, admin: dict = Depends(require_admin)):
    await clear_session(session_id)
    return {"session_id": session_id, "cleared": True}


# ── 导出 ────────────────────────────────────────────────────────
@router.get("/export/conversations.csv")
async def export_conversations(
    user_id: str = Query(default=""),
    route: str = Query(default=""),
    keyword: str = Query(default=""),
    day: str = Query(default=""),
    page_size: int = Query(default=100, ge=1, le=100),
    admin: dict = Depends(require_admin),
):
    data = await chatlog.list_turns(
        user_id=user_id, route=route, keyword=keyword, day=day, page=1, page_size=page_size
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["时间(UTC)", "用户", "用户ID", "入口", "会话", "问题", "回答",
                     "模型", "Token", "耗时(ms)", "状态", "检索次数", "trace_id"])
    for item in data["items"]:
        writer.writerow([
            datetime.fromtimestamp(int(item.get("ts") or 0) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            item.get("user_name") or "匿名",
            item.get("user_id") or "",
            item.get("route") or "",
            item.get("session_id") or "",
            item.get("question") or "",
            item.get("answer") or "",
            item.get("model") or "",
            item.get("total_tokens") or 0,
            item.get("latency_ms") or 0,
            item.get("status") or "",
            len(item.get("retrievals") or []),
            item.get("trace_id") or "",
        ])
    buffer.seek(0)
    # BOM 让 Excel 正确识别 UTF-8 中文
    content = "\ufeff" + buffer.getvalue()
    filename = f"conversations-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.csv"
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── 提示词安全防护 ──────────────────────────────────────────────
class SecurityCheckPayload(BaseModel):
    text: str


@router.get("/security")
async def security_status(
    limit: int = Query(default=30, ge=1, le=200),
    admin: dict = Depends(require_admin),
):
    """安全模块的配置、统计与最近拦截事件"""
    return {
        "config": security_guard.config(),
        "stats": await security_store.get_stats(),
        "events": await security_store.list_events(limit),
    }


@router.post("/security/check")
async def security_check(payload: SecurityCheckPayload, admin: dict = Depends(require_admin)):
    """规则自测：拿一段文字过一遍防护链路，看走的是哪一层、为什么"""
    verdict = await security_guard.check(payload.text or "", route="admin-test")
    return {
        "allowed": verdict.allowed,
        "blocked": verdict.blocked,
        "layer": verdict.layer,
        "category": verdict.category,
        "reason": verdict.reason,
        "latency_ms": verdict.latency_ms,
        "cached": verdict.cached,
        "model": verdict.model,
        "model_tokens": verdict.model_tokens,
        "message": verdict.message,
        "details": verdict.details,
    }


@router.delete("/security/events")
async def security_clear_events(admin: dict = Depends(require_admin)):
    return {"cleared": await security_store.clear_events()}


@router.delete("/security/stats")
async def security_reset_stats(admin: dict = Depends(require_admin)):
    await security_store.reset_stats()
    return {"cleared": True}


# ── 系统状态 ────────────────────────────────────────────────────
def _postgres_stats() -> dict:
    """同步查询知识库表（在线程里跑，避免阻塞事件循环）"""
    import psycopg

    from app.db.postgres import PG_CONNECTION_STRING

    dsn = PG_CONNECTION_STRING.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from langchain_pg_embedding")
            chunks = int(cur.fetchone()[0])
            cur.execute("select count(distinct cmetadata->>'source') from langchain_pg_embedding")
            sources = int(cur.fetchone()[0])
    return {"connected": True, "chunks": chunks, "sources": sources}


@router.get("/system")
async def system_status(admin: dict = Depends(require_admin)):
    from app.retrieval import rerank as rerank_module
    from app.retrieval.rag_chain import COLLECTION_NAME, RAG_SCORE_THRESHOLD, RAG_TOP_K
    from app.db.postgres import PG_DATABASE, PG_HOST, PG_PORT
    from app.models.deepseek import DEEPSEEK_BASE_URL, MODEL_NAME

    postgres: dict = {"connected": False, "host": PG_HOST, "port": PG_PORT, "database": PG_DATABASE}
    try:
        postgres.update(await asyncio.to_thread(_postgres_stats))
    except Exception as err:
        postgres["error"] = str(err)[:200]

    return {
        "service": {"name": "极速购 AI 客服系统", "version": "1.2.0",
                    "server_time": datetime.now(timezone.utc).isoformat()},
        "redis": {
            "connected": await redis_client.ping(),
            "host": redis_client.REDIS_HOST,
            "port": redis_client.REDIS_PORT,
            "session_ttl_seconds": SESSION_TTL_SECONDS,
        },
        "postgres": postgres,
        "llm": {
            "model": MODEL_NAME,
            "base_url": DEEPSEEK_BASE_URL,
            "fallback_model": os.getenv("FALLBACK_MODEL_NAME", "") or "(未配置)",
        },
        "rag": {
            "collection": COLLECTION_NAME,
            "top_k": RAG_TOP_K,
            "score_threshold": RAG_SCORE_THRESHOLD,
            "rerank": rerank_module.describe(),
        },
        "admin": {
            "username": ADMIN_USERNAME,
            "token_ttl_seconds": ADMIN_TOKEN_TTL,
        },
        "retention": {
            "usage_seconds": USAGE_RETENTION_SECONDS,
            "chatlog_seconds": chatlog.CHATLOG_RETENTION_SECONDS,
            "chatlog_scan_limit": chatlog.CHATLOG_SCAN_LIMIT,
        },
        "circuit": circuit_snapshot(),
    }


# ─── 熔断状态（可观测 + 手动运维）────────────────────────────────
def circuit_snapshot() -> dict:
    """各依赖的熔断状态；只读，任何异常都不影响接口"""
    from app.resilience import circuit

    try:
        items = circuit.snapshot_all()
    except Exception as err:  # pragma: no cover - 防御式
        return {"enabled": False, "error": str(err)[:200], "items": []}

    return {
        "enabled": circuit.circuit_enabled(),
        "dry_run": circuit.circuit_dry_run(),
        "config": {
            "window_seconds": float(os.getenv("CIRCUIT_WINDOW_SECONDS", "60")),
            "min_requests": int(os.getenv("CIRCUIT_MIN_REQUESTS", "10")),
            "failure_rate": float(os.getenv("CIRCUIT_FAILURE_RATE", "0.5")),
            "consecutive_failures": int(os.getenv("CIRCUIT_CONSECUTIVE_FAILURES", "5")),
            "open_seconds": float(os.getenv("CIRCUIT_OPEN_SECONDS", "30")),
            "backoff_max_seconds": float(os.getenv("CIRCUIT_BACKOFF_MAX_SECONDS", "300")),
            "half_open_probes": int(os.getenv("CIRCUIT_HALF_OPEN_PROBES", "3")),
        },
        "items": sorted(items, key=lambda item: item["name"]),
    }


@router.get("/circuit")
async def circuit_status(admin: dict = Depends(require_admin)):
    """熔断状态（管理员后台「系统状态」页展示）"""
    return circuit_snapshot()


@router.post("/circuit/{name}/reset")
async def circuit_reset(name: str, admin: dict = Depends(require_admin)):
    """手动复位某个依赖的熔断（下游已确认恢复，不想等冷却）"""
    from app.resilience import get_breaker

    breaker = get_breaker(name)
    breaker.reset()
    return {"ok": True, "name": name, "action": "reset", "snapshot": breaker.snapshot()}


@router.post("/circuit/{name}/half-open")
async def circuit_force_half_open(name: str, admin: dict = Depends(require_admin)):
    """强制进入半开，立刻放一个探测请求"""
    from app.resilience import get_breaker

    breaker = get_breaker(name)
    breaker.force_half_open()
    return {"ok": True, "name": name, "action": "half_open", "snapshot": breaker.snapshot()}
