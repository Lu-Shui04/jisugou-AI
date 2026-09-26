"""开屏门禁单测：Token 不可伪造 + challenge 一次性 + 人机下限 + 依赖真的挂在烧钱入口上

对应这次加的层：滑块验证不是纯前端装饰 ——
纯前端 F12 就能绕过，脚本直接打 /api/agent/stream 照样把 API 额度刷光，
所以"拖动结果要过服务端、Token 要服务端签、烧钱入口要校验"这三件事都得有测试兜着。
"""
import asyncio
import unittest

from fastapi import HTTPException

from app.db import redis_client
from app.security import gate


def run(coro):
    return asyncio.run(coro)


# ── 1. Token：签发 / 验签 / 改一个字节就作废 ─────────────────────
class TestGateToken(unittest.TestCase):
    def test_issue_and_verify(self):
        token = gate.issue_token()["token"]
        self.assertTrue(gate.verify_token(token))

    def test_expired_token_is_rejected(self):
        token = gate.issue_token(ttl_seconds=-5)["token"]
        self.assertFalse(gate.verify_token(token))

    def test_tampered_signature_is_rejected(self):
        token = gate.issue_token()["token"]
        broken = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
        self.assertFalse(gate.verify_token(broken))

    def test_forged_expiry_is_rejected(self):
        """自己往后写一个过期时间也不行：没有密钥签不出签名"""
        self.assertFalse(gate.verify_token("9999999999." + "0" * 32))

    def test_malformed_tokens(self):
        for bad in ("", "abc", "123", "abc.def", ".", "123."):
            with self.subTest(bad=bad):
                self.assertFalse(gate.verify_token(bad))


# ── 2. challenge：一次性 + 人类拖动的下限 ────────────────────────
class TestChallenge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # async Redis 连接池绑定在"创建它的那个 event loop"上，
        # 而 IsolatedAsyncioTestCase 每个用例换一个新 loop：
        # 不丢掉旧连接的话，第二个用例会拿到上一个 loop 的连接，出现偶发失败
        # （本文件第一版就是这么挂的：4 个用例报"challenge 不存在或已使用"）。
        redis_client._client = None

    async def _new(self):
        return (await gate.new_challenge())["challenge_id"]

    async def test_human_drag_passes(self):
        ok, reason = await gate.verify_challenge(await self._new(), 800, 24)
        self.assertTrue(ok, reason)

    async def test_challenge_is_one_time(self):
        """同一个 challenge 用第二次必须失败（防止一次拖动换多枚 Token）"""
        cid = await self._new()
        self.assertTrue((await gate.verify_challenge(cid, 800, 24))[0])
        ok, reason = await gate.verify_challenge(cid, 800, 24)
        self.assertFalse(ok)
        self.assertIn("challenge", reason)

    async def test_unknown_challenge_is_rejected(self):
        ok, _reason = await gate.verify_challenge("not-a-real-challenge", 800, 24)
        self.assertFalse(ok)

    async def test_too_fast_is_rejected(self):
        """脚本瞬间拖到底：200ms 以下直接拒"""
        ok, reason = await gate.verify_challenge(await self._new(), 30, 99)
        self.assertFalse(ok)
        self.assertEqual(reason, "拖动过快")

    async def test_too_slow_is_rejected(self):
        ok, reason = await gate.verify_challenge(await self._new(), 60_000, 99)
        self.assertFalse(ok)
        self.assertEqual(reason, "拖动超时")

    async def test_missing_track_is_rejected(self):
        """纯点击（0 轨迹点）不算拖动"""
        ok, reason = await gate.verify_challenge(await self._new(), 800, 0)
        self.assertFalse(ok)
        self.assertEqual(reason, "缺少拖动轨迹")

    async def test_failed_attempt_still_burns_challenge(self):
        """失败也作废：不能拿同一个 challenge 反复试到过"""
        cid = await self._new()
        self.assertFalse((await gate.verify_challenge(cid, 10, 1))[0])
        self.assertFalse((await gate.verify_challenge(cid, 900, 30))[0])


# ── 3. 依赖：没带 Token 就是 401 ─────────────────────────────────
class TestRequireGate(unittest.TestCase):
    def test_missing_token_gives_401(self):
        with self.assertRaises(HTTPException) as ctx:
            run(gate.require_gate(""))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_bad_token_gives_401(self):
        with self.assertRaises(HTTPException) as ctx:
            run(gate.require_gate("9999999999.deadbeef"))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_valid_token_passes(self):
        self.assertTrue(run(gate.require_gate(gate.issue_token()["token"])))


# ── 4. 接线：四个烧钱入口真的挂了门禁，健康检查没挂 ───────────────
class TestRouteWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from app.routers import agent, chat, graph, rag
        except Exception as err:  # 没有模型配置的环境（纯离线跑单测）就跳过接线检查
            raise unittest.SkipTest("无法导入路由模块: %s" % err)
        cls.routers = {"chat": chat.router, "agent": agent.router,
                       "rag": rag.router, "graph": graph.router}

    @staticmethod
    def _find(router, path, method):
        for route in router.routes:
            if getattr(route, "path", "") == path and method in getattr(route, "methods", set()):
                return route
        return None

    @staticmethod
    def _protected(route) -> bool:
        for dep in getattr(route.dependant, "dependencies", []):
            if getattr(dep, "call", None) is gate.require_gate:
                return True
        return False

    def test_costly_routes_require_gate(self):
        cases = [("chat", "/stream", "POST"), ("agent", "/stream", "POST"),
                 ("rag", "/query", "POST"), ("graph", "/stream", "POST")]
        for name, path, method in cases:
            with self.subTest(route=name + path):
                route = self._find(self.routers[name], path, method)
                self.assertIsNotNone(route, "%s %s 路由不存在" % (method, path))
                self.assertTrue(self._protected(route), "%s %s 没挂门禁" % (method, path))

    def test_health_is_not_gated(self):
        """健康检查要留给部署自检和监控，不能要求滑块 Token"""
        route = self._find(self.routers["chat"], "/health", "GET")
        self.assertIsNotNone(route)
        self.assertFalse(self._protected(route))
