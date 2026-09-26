"""开屏人机验证（滑块）—— 无密码的轻量防滥用层。

为什么不用密码：这个站是给人点开看的（简历 / 演示），密码会卡住第一屏，
而且密码写出去等于没有。

为什么不能只做前端滑块：纯前端校验 F12 就能绕过，写脚本的人直接调
/api/agent/stream、/api/graph/stream 照样把 API 额度刷光。
所以滑块结束后必须由**服务端签发签名 Token**，烧钱的入口都校验它。

流程：
    1. GET  /api/gate/challenge  -> 下发一次性 challenge（默认 5 分钟）
    2. 前端拖动滑块，记录轨迹点数与耗时
    3. POST /api/gate/verify     -> 合理性校验通过后签发 HMAC Token（默认 12 小时）
    4. 之后受保护入口带 X-Gate-Token 头

和"单进程版本"的一个关键区别：challenge 存 **Redis**，不是进程内字典。
线上是 uvicorn --workers 2，进程内存各存各的：challenge 由 A 进程下发、
校验却落在 B 进程时，就会出现"challenge 不存在或已使用"——用户第一次拖必然偶发失败。
Redis 不可用时退回进程内字典（退化状态，前端失败后会自动重试一次）。

诚实说明：这不是强安全。它挡的是扫描器和顺手薅羊毛的脚本，
挡不住铁了心要打的人；对"公开演示站"这个场景，够用且体验好。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import Header, HTTPException

logger = logging.getLogger("jisu.gate")

# Token 有效期：默认 12 小时（一次会话够用）
TOKEN_TTL = int(os.getenv("GATE_TOKEN_TTL", str(12 * 3600)))
# challenge 有效期：5 分钟，够用户拖一下
CHALLENGE_TTL = int(os.getenv("GATE_CHALLENGE_TTL", "300"))

# 人类拖完一整条滑块至少要 200ms（脚本常见 <50ms）；超过 30s 说明页面早没人了
MIN_DRAG_MS = 200
MAX_DRAG_MS = 30_000
# 至少要记录到几次移动：纯点击不产生轨迹
MIN_DRAG_POINTS = 5

# 签名密钥：生产必须在 .env 里固定。不固定的话，重启换密钥 + 两个 worker 各签各的，
# 已经进站的用户会莫名其妙被弹回滑块。
GATE_SECRET = os.getenv("GATE_SECRET") or secrets.token_urlsafe(32)
if not os.getenv("GATE_SECRET"):
    logger.warning("未配置 GATE_SECRET，使用随机密钥（重启后已有 Token 失效，多 worker 下会互相不认）")

CHALLENGE_KEY = "gate:challenge:{cid}"

# Redis 不可用时的兜底（只在单进程内有效）
_memory_challenges: dict[str, float] = {}


def _key(challenge_id: str) -> str:
    return CHALLENGE_KEY.format(cid=challenge_id)


def _sign(payload: str) -> str:
    return hmac.new(GATE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(ttl_seconds: int | None = None) -> dict:
    """签发门禁 Token：payload 就是过期时间戳，签名防伪造"""
    ttl = TOKEN_TTL if ttl_seconds is None else ttl_seconds
    exp = int(time.time()) + ttl
    payload = str(exp)
    return {"token": payload + "." + _sign(payload), "expires_in": ttl}


def verify_token(token: str) -> bool:
    """验 Token：格式对 + 没过期 + 签名对（改一个字节就废）"""
    if not token or "." not in token:
        return False
    payload, _, signature = token.partition(".")
    try:
        exp = int(payload)
    except ValueError:
        return False
    if exp < time.time():
        return False
    return hmac.compare_digest(signature, _sign(payload))


def _memory_cleanup() -> None:
    now = time.time()
    for cid in [key for key, exp in _memory_challenges.items() if exp < now]:
        _memory_challenges.pop(cid, None)


async def _store_challenge(challenge_id: str) -> str:
    """写入 challenge，返回实际落到的存储（redis / memory，后者是降级）"""
    from app.db import redis_client

    _memory_cleanup()
    try:
        async with redis_client.get_circuit().aguard():
            await redis_client.get_redis().set(_key(challenge_id), "1", ex=CHALLENGE_TTL)
        return "redis"
    except Exception as err:
        logger.warning("challenge 写 Redis 失败，降级为进程内存储: %s", err)
        _memory_challenges[challenge_id] = time.time() + CHALLENGE_TTL
        return "memory"


async def _consume_challenge(challenge_id: str) -> bool:
    """取走 challenge（一次性：取过就作废）。取不到 / 已过期都算失败。"""
    from app.db import redis_client

    # 先看降级期间存在本进程里的
    exp = _memory_challenges.pop(challenge_id, None)
    if exp is not None:
        return exp >= time.time()
    try:
        async with redis_client.get_circuit().aguard():
            pipe = redis_client.get_redis().pipeline()
            pipe.get(_key(challenge_id))
            pipe.delete(_key(challenge_id))
            value, _ = await pipe.execute()
        return bool(value)
    except Exception as err:
        logger.warning("读取 challenge 失败: %s", err)
        return False


async def new_challenge() -> dict:
    """下发一次性 challenge"""
    challenge_id = secrets.token_urlsafe(18)
    store = await _store_challenge(challenge_id)
    return {"challenge_id": challenge_id, "expires_in": CHALLENGE_TTL, "store": store}


async def verify_challenge(challenge_id: str, duration_ms: int, points: int) -> tuple[bool, str]:
    """校验滑块结果，返回 (是否通过, 原因)"""
    if not await _consume_challenge(challenge_id):
        return False, "challenge 不存在或已使用"
    if duration_ms < MIN_DRAG_MS:
        return False, "拖动过快"
    if duration_ms > MAX_DRAG_MS:
        return False, "拖动超时"
    if points < MIN_DRAG_POINTS:
        return False, "缺少拖动轨迹"
    return True, "ok"


async def require_gate(x_gate_token: str = Header(default="", alias="X-Gate-Token")) -> bool:
    """受保护入口的依赖：必须带一枚有效的滑块 Token

    没带 / 过期 / 签名不对 → 401，前端据此把用户弹回开屏滑块。
    """
    if not verify_token(x_gate_token):
        raise HTTPException(status_code=401, detail="需要先完成滑块验证")
    return True


# 只挡会真的调大模型 / 检索的四个入口：chat / agent / rag / graph
# （见 routers/*.py 上的 dependencies=[Depends(require_gate)]）。
# 健康检查、身份、后台观测不挡：它们不烧额度，挡了会把部署自检和监控一起关在门外。
