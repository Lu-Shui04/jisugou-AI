"""身份认证：说话的人到底是谁（权限系统的地基）

补的洞（真实攻击路径）：
    改造前，整条链路里没有任何"我是谁"的凭证 —— 前端在请求体里塞一个 user_id，
    后端就照着这个 user_id 去查订单。也就是说，任何人把请求体里的
    {"user_id": "U-100"} 改成 {"user_id": "U-103"}，就能看到 U-103 的订单列表、
    订单详情、物流轨迹（典型的水平越权 / IDOR）。

现在的规则（三句话）：
    1. 身份只在「登录」时确定一次（左上角切换用户 = 登录），由服务端签发 HMAC 令牌；
    2. 之后每个请求带的是**令牌**而不是 ID：后端只认令牌签名里的 sub，
       请求体里的 user_id / user_name 一律只当"日志昵称"，绝不参与鉴权；
    3. 令牌是 HMAC-SHA256 签名的，客户端改一个字节就验签失败 → 退化成匿名 → 查不到数据。

真实系统里登录环节是账号密码 / SSO / 短信验证码；这里是演示环境，用"选择演示用户"
代替登录，但令牌的签发、验签、过期、防篡改与生产一致（生产必须配 IDENTITY_SECRET）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger("jisu.identity")

TOKEN_PREFIX = "jisu1"
# 令牌有效期：12 小时（前端拿到 401 会自动用原身份重新登录，用户无感）
TOKEN_TTL_SECONDS = int(os.getenv("IDENTITY_TTL_SECONDS", "43200"))
MAX_INCIDENTS = 200
# 权限事件落盘路径：容器是 uvicorn --workers 2，内存里的 list 每个进程各一份，
# 越权记录会"这次查得到、下次查不到"。落成一个共享文件，多进程/重启都不丢。
INCIDENT_LOG_PATH = os.getenv("IDENTITY_INCIDENT_LOG") or os.path.join(
    tempfile.gettempdir(), "jisu_identity_incidents.jsonl")
INCIDENT_LOG_MAX_BYTES = 512 * 1024
INCIDENT_LOG_KEEP_LINES = 1000

LOGIN_REQUIRED_MESSAGE = (
    "亲，订单数据只能由本人查看，麻烦先在页面左上角选择用户身份（U-100 ~ U-104），"
    "小购确认了您是谁才能帮您查～"
)
SESSION_EXPIRED_MESSAGE = (
    "亲，您的登录状态已过期，请在页面左上角重新选择用户身份后再查订单～"
)


# ── 演示用户表（真实系统里这张表在账号库）──────────────────────────
@dataclass(frozen=True)
class DemoUser:
    user_id: str
    name: str
    note: str = ""

    def as_dict(self) -> dict:
        return {"user_id": self.user_id, "name": self.name, "note": self.note}


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("U-100", "张伟"),
    DemoUser("U-101", "李娜"),
    DemoUser("U-102", "王强"),
    DemoUser("U-103", "刘洋"),
    DemoUser("U-104", "陈静", "暂无订单"),
)
_USERS: dict[str, DemoUser] = {user.user_id: user for user in DEMO_USERS}


def default_user_id() -> str:
    return DEMO_USERS[0].user_id


def known_user_ids() -> list[str]:
    return [user.user_id for user in DEMO_USERS]


def list_users() -> list[dict]:
    return [user.as_dict() for user in DEMO_USERS]


def get_user(user_id: str) -> Optional[DemoUser]:
    return _USERS.get((user_id or "").strip().upper().replace(" ", ""))


def display_name(user_id: str) -> str:
    user = get_user(user_id)
    return user.name if user else (user_id or "匿名")


# ── 身份主体（Principal）：一次请求里"我是谁"的唯一事实来源 ──────────
@dataclass(frozen=True)
class Principal:
    user_id: str = ""
    user_name: str = ""
    authenticated: bool = False
    # 未通过校验的原因：no_token / malformed / bad_signature / expired / unknown_user
    reason: str = "no_token"
    exp: int = 0
    jti: str = ""

    @property
    def anonymous(self) -> bool:
        return not self.authenticated

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "user_name": self.user_name,
            "authenticated": self.authenticated,
            "reason": self.reason,
            "exp": self.exp,
        }

    def denial_message(self) -> str:
        """没登录（或登录过期）时给用户看的话术"""
        return SESSION_EXPIRED_MESSAGE if self.reason in ("expired",) else LOGIN_REQUIRED_MESSAGE


ANONYMOUS = Principal()


# ── 权限事件：越权尝试要留痕（谁、想查谁、什么时候）────────────────
_INCIDENTS: deque = deque(maxlen=MAX_INCIDENTS)


def _trim_incident_log(path: str) -> None:
    """文件太大就只留最后若干行（权限事件不是账本，不需要无限增长）"""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()[-INCIDENT_LOG_KEEP_LINES:]
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)


def _append_incident_line(event: dict) -> None:
    """追加一行到共享事件文件（失败只记 debug 日志，绝不影响主流程）"""
    try:
        path = INCIDENT_LOG_PATH
        try:
            if os.path.getsize(path) > INCIDENT_LOG_MAX_BYTES:
                _trim_incident_log(path)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as err:  # 磁盘满 / 只读文件系统…
        logger.debug("权限事件落盘失败: %s", err)


def _read_incident_log(limit: int) -> list[dict]:
    try:
        with open(INCIDENT_LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    events: list[dict] = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(events))  # 新的在前


def record_incident(kind: str, **detail) -> dict:
    """记一条权限事件。越权不是普通报错，必须能被审计到（并且多进程都看得到）。"""
    event = {"ts": int(time.time() * 1000), "kind": kind,
             **{key: value for key, value in detail.items() if value not in (None, "")}}
    _INCIDENTS.append(event)
    _append_incident_line(event)
    logger.warning("权限事件 %s %s", kind, json.dumps(event, ensure_ascii=False))
    return event


def audit_claim(principal: "Principal", claimed: str, route: str = "") -> bool:
    """请求体里客户端"声称"的 user_id 与令牌身份不符时留痕

    注意：**只留痕，不影响鉴权** —— 鉴权永远只看令牌。有人把 user_id 改成别人，
    就是一次典型的越权尝试，必须能被查到。
    """
    claimed_id = (claimed or "").strip().upper().replace(" ", "")
    if not claimed_id or claimed_id == principal.user_id:
        return False
    record_incident("identity_spoof", actor=principal.user_id or "anonymous",
                    claimed=claimed_id, route=route)
    return True


def recent_incidents(limit: int = 50) -> list[dict]:
    """最近的权限事件（新的在前），供前端 / 后台查看

    以共享文件为准（多 worker 写的都在里面）；文件读不到才退回本进程内存。
    """
    size = max(1, int(limit))
    events = _read_incident_log(size)
    if events:
        return events
    return list(reversed(list(_INCIDENTS)[-size:]))


def incident_counts() -> dict:
    counts: dict[str, int] = {}
    for event in recent_incidents(limit=INCIDENT_LOG_KEEP_LINES):
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return counts


# ── 令牌签发 / 校验 ───────────────────────────────────────────────
def _load_secret() -> bytes:
    secret = (os.getenv("IDENTITY_SECRET") or "").strip()
    if secret:
        return secret.encode("utf-8")
    # 没配就随机生成：绝不把密钥写死在源码里（那等于没有密钥）。
    # 代价必须说清楚：每个进程一份随机密钥 —— 容器是 uvicorn --workers 2，
    # 两个进程互相验不过对方的令牌，表现就是"登录态时好时坏"。
    logger.warning(
        "未配置 IDENTITY_SECRET：本次启动使用**进程内随机密钥**。"
        "多 worker 部署时各进程密钥不同，令牌会随机验签失败（用户表现为登录态莫名失效）；"
        "进程重启后旧令牌也会全部失效。生产环境必须在 .env 配置 IDENTITY_SECRET。"
    )
    return secrets.token_bytes(32)


_SECRET = _load_secret()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64e(hmac.new(_SECRET, body.encode("ascii"), hashlib.sha256).digest())


def issue_token(user_id: str, ttl_seconds: Optional[int] = None) -> str:
    """给某个用户签发令牌（只有这里能"决定我是谁"，服务端说了算）"""
    user = get_user(user_id)
    if user is None:
        raise ValueError(f"用户 {user_id} 不存在")
    now = int(time.time())
    payload = {
        "sub": user.user_id,
        "name": user.name,
        "iat": now,
        "exp": now + int(ttl_seconds or TOKEN_TTL_SECONDS),
        "jti": uuid.uuid4().hex[:12],
    }
    body = _b64e(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{TOKEN_PREFIX}.{body}.{_sign(body)}"


def verify_token(token: str) -> tuple[Optional[Principal], str]:
    """校验令牌，返回 (身份, 失败原因)

    失败一律返回 None + 原因，调用方按"匿名"处理（fail closed）。
    """
    raw = (token or "").strip()
    if not raw:
        return None, "no_token"
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None, "malformed"
    _, body, signature = parts
    # 用 compare_digest 做定时安全比较：普通 == 会被逐字节计时攻击
    if not hmac.compare_digest(_sign(body), signature):
        return None, "bad_signature"
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None, "malformed"
    if not isinstance(payload, dict):
        return None, "malformed"
    user = get_user(str(payload.get("sub") or ""))
    if user is None:
        return None, "unknown_user"
    exp = int(payload.get("exp") or 0)
    if exp <= int(time.time()):
        return None, "expired"
    return Principal(
        user_id=user.user_id,
        user_name=user.name,
        authenticated=True,
        reason="ok",
        exp=exp,
        jti=str(payload.get("jti") or ""),
    ), "ok"


# ── 当前请求的身份（ContextVar：每个请求一份，不会串）──────────────
_current: ContextVar[Principal] = ContextVar("jisu_principal", default=ANONYMOUS)


def current_principal() -> Principal:
    """当前请求的真实身份。工具、节点、会话缓存都只认它，不认请求参数。"""
    return _current.get()


def current_user_id() -> str:
    return _current.get().user_id


def set_principal(principal: Principal):
    return _current.set(principal or ANONYMOUS)


def reset_principal(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):  # 跨上下文 reset 时忽略
        pass


@contextmanager
def principal_scope(principal: Principal) -> Iterator[Principal]:
    """with principal_scope(p): ... —— 给后台任务 / 测试用"""
    token = set_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


@contextmanager
def acting_as(who) -> Iterator[Principal]:
    """临时以某个身份执行（测试 / 内部直调工具时用）

    传字符串或 Principal 都可以；用户不存在直接抛错，避免"以为在跑某身份"其实没有。
    """
    if isinstance(who, Principal):
        principal = who
    else:
        user = get_user(str(who))
        if user is None:
            raise ValueError(f"用户 {who} 不存在，无法以该身份执行")
        principal = Principal(user_id=user.user_id, user_name=user.name,
                              authenticated=True, reason="ok",
                              exp=int(time.time()) + TOKEN_TTL_SECONDS)
    token = set_principal(principal)
    try:
        yield principal
    finally:
        reset_principal(token)


def bearer_token(headers) -> str:
    """从 ASGI 头里取令牌：Authorization: Bearer xxx，或退化用 X-User-Token"""
    authorization = ""
    fallback = ""
    for raw_name, raw_value in headers or []:
        name = (raw_name or b"").decode("latin-1").lower()
        value = (raw_value or b"").decode("latin-1").strip()
        if name == "authorization":
            authorization = value
        elif name == "x-user-token":
            fallback = value
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            return credential.strip()
        # 客户端只把裸令牌塞进 Authorization 时也认（容错，不影响安全性）
        return authorization
    return fallback
