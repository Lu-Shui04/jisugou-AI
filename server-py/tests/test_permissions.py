"""权限单测：身份不可伪造 + 订单只能查本人 + 会话不串用户 + 中间件真的在鉴权

对应这次补的洞：改造前整条链路没有任何"我是谁"的凭证，前端在请求体里塞谁就是谁 ——
把 user_id 改成 U-103 就能看 U-103 的订单（水平越权 / IDOR）。
"""
import asyncio
import contextlib
import json
import time
import unittest

from app.db import redis_client
from app.security import access, identity
from app.security.middleware import IdentityMiddleware
from app.tools.order_tools import (
    get_logistics_tool,
    get_order_info_tool,
    get_user_orders_tool,
)


def _json(payload):
    return payload if isinstance(payload, dict) else json.loads(payload)


# ── 1. 令牌：改一个字节就作废 ─────────────────────────────────────
class TestTokenSecurity(unittest.TestCase):
    def test_issue_and_verify(self):
        token = identity.issue_token("U-103")
        principal, reason = identity.verify_token(token)
        self.assertEqual(reason, "ok")
        self.assertTrue(principal.authenticated)
        self.assertEqual(principal.user_id, "U-103")

    def test_tampered_body_is_rejected(self):
        """把令牌里的 sub 改成别人：验签直接失败"""
        token = identity.issue_token("U-100")
        prefix, body, signature = token.split(".")
        forged_body = identity._b64e(json.dumps(
            {"sub": "U-103", "exp": int(time.time()) + 3600}, ensure_ascii=False).encode())
        forged = f"{prefix}.{forged_body}.{signature}"
        principal, reason = identity.verify_token(forged)
        self.assertIsNone(principal)
        self.assertEqual(reason, "bad_signature")

    def test_tampered_signature_is_rejected(self):
        token = identity.issue_token("U-100")
        broken = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
        principal, reason = identity.verify_token(broken)
        self.assertIsNone(principal)
        self.assertEqual(reason, "bad_signature")

    def test_expired_token_is_rejected(self):
        token = identity.issue_token("U-100", ttl_seconds=-10)
        principal, reason = identity.verify_token(token)
        self.assertIsNone(principal)
        self.assertEqual(reason, "expired")

    def test_malformed_token(self):
        for bad in ("", "abc", "jisu1.xxx", "jisu1.xxx.yyy.zzz"):
            principal, reason = identity.verify_token(bad)
            self.assertIsNone(principal, bad)

    def test_signed_token_for_unknown_user(self):
        """签名对但账号不在册（比如账号被删了）→ 不认"""
        body = identity._b64e(json.dumps(
            {"sub": "U-999", "exp": int(time.time()) + 3600}, ensure_ascii=False).encode())
        token = f"{identity.TOKEN_PREFIX}.{body}.{identity._sign(body)}"
        principal, reason = identity.verify_token(token)
        self.assertIsNone(principal)
        self.assertEqual(reason, "unknown_user")

    def test_cannot_issue_token_for_unknown_user(self):
        with self.assertRaises(ValueError):
            identity.issue_token("U-999")


# ── 2. 订单数据：只能查本人 ───────────────────────────────────────
class TestOrderDataScope(unittest.TestCase):
    def test_own_orders_ok(self):
        with identity.acting_as("U-100"):
            payload = _json(get_user_orders_tool.invoke({"userId": "U-100"}))
        ids = [item["orderId"] for item in payload]
        self.assertIn("ORD-001", ids)

    def test_query_other_user_forbidden(self):
        """u-100 查 u-103 的订单列表：拒绝，且返回里不能出现任何订单号"""
        with identity.acting_as("U-100"):
            payload = _json(get_user_orders_tool.invoke({"userId": "U-103"}))
        self.assertEqual(payload["code"], "FORBIDDEN")
        self.assertNotIn("ORD-008", json.dumps(payload, ensure_ascii=False))

    def test_query_other_order_forbidden_without_data(self):
        """u-100 直接报 u-103 的订单号：拒绝，并且一个字段都不给（状态/金额/商品）"""
        with identity.acting_as("U-100"):
            payload = _json(get_order_info_tool.invoke({"orderId": "ORD-008"}))
        self.assertEqual(payload["code"], "FORBIDDEN")
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("已发货", text)
        self.assertNotIn("1388", text)
        self.assertNotIn("降噪头戴耳机", text)

    def test_query_other_tracking_forbidden(self):
        """绕过订单号、直接报快递单号 —— 这条路也必须堵上"""
        with identity.acting_as("U-100"):
            payload = _json(get_logistics_tool.invoke({"trackingNo": "SF9988776655"}))
        self.assertEqual(payload["code"], "FORBIDDEN")
        self.assertNotIn("records", payload)

    def test_own_tracking_ok(self):
        with identity.acting_as("U-103"):
            payload = _json(get_logistics_tool.invoke({"trackingNo": "SF9988776655"}))
        self.assertIn("records", payload)
        self.assertTrue(payload["records"])

    def test_omitted_user_id_defaults_to_self(self):
        """模型没传 userId（用户只说"我有哪些订单"）→ 默认查本人"""
        with identity.acting_as("U-101"):
            payload = _json(get_user_orders_tool.invoke({}))
        ids = [item["orderId"] for item in payload]
        self.assertIn("ORD-003", ids)
        self.assertNotIn("ORD-001", ids)  # ORD-001 是 U-100 的

    def test_anonymous_gets_nothing(self):
        """没登录：三个工具全部拒绝，且不带任何数据"""
        for call in (lambda: get_user_orders_tool.invoke({"userId": "U-100"}),
                     lambda: get_order_info_tool.invoke({"orderId": "ORD-001"}),
                     lambda: get_logistics_tool.invoke({"trackingNo": "SF1234567890"})):
            payload = _json(call())
            self.assertEqual(payload["code"], "UNAUTHENTICATED")
            self.assertNotIn("ORD-001", json.dumps(payload, ensure_ascii=False))

    def test_denial_is_audited(self):
        """越权尝试要留痕（谁、想查什么）"""
        before = len(identity.recent_incidents(limit=200))
        with identity.acting_as("U-100"):
            get_order_info_tool.invoke({"orderId": "ORD-008"})
        after = identity.recent_incidents(limit=5)
        self.assertGreater(len(identity.recent_incidents(limit=200)), before - 1)
        actors = [event for event in after if event["kind"] == "order_scope_denied"]
        self.assertTrue(actors)
        self.assertEqual(actors[0]["actor"], "U-100")


# ── 3. 请求体里的身份参数：只当"声称"，不当事实 ─────────────────────
class TestClaimedIdentityIsIgnored(unittest.TestCase):
    def test_claimed_id_cannot_change_scope(self):
        """令牌是 U-100，工具入参写 U-103 → 依然拒绝（入参不是鉴权依据）"""
        with identity.acting_as("U-100"):
            payload = _json(get_user_orders_tool.invoke({"userId": "U-103"}))
        self.assertEqual(payload["code"], "FORBIDDEN")

    def test_claim_is_audited(self):
        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        self.assertTrue(identity.audit_claim(principal, "U-103", route="agent"))
        events = [e for e in identity.recent_incidents(limit=10) if e["kind"] == "identity_spoof"]
        self.assertTrue(events)
        self.assertEqual(events[0]["claimed"], "U-103")

    def test_matching_claim_is_not_an_incident(self):
        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        self.assertFalse(identity.audit_claim(principal, "u-100", route="agent"))


# ── 4. 会话缓存：不能读到别人的上下文 ─────────────────────────────
class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


class _OpenCircuit:
    def aguard(self):
        @contextlib.asynccontextmanager
        async def _cm():
            yield self
        return _cm()


class TestSessionOwnership(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = _FakeRedis()
        self._client = redis_client._client
        self._circuit = redis_client.get_circuit
        redis_client._client = self.fake
        redis_client.get_circuit = lambda: _OpenCircuit()

    def tearDown(self):
        redis_client._client = self._client
        redis_client.get_circuit = self._circuit

    async def test_history_is_scoped_to_owner(self):
        await redis_client.append_turn("s1", "我的订单呢", "ORD-001 已发货", owner="U-100")
        self.assertEqual(len(await redis_client.get_history("s1", owner="U-100")), 2)
        # 换个身份拿同一个 session_id：什么都读不到（并且留痕）
        self.assertEqual(await redis_client.get_history("s1", owner="U-103"), [])
        # 后台（owner=None）才能看到全部
        self.assertEqual(len(await redis_client.get_history("s1")), 2)

    async def test_legacy_payload_without_owner_is_not_trusted(self):
        from app.db.redis_client import session_key
        self.fake.store[session_key("s2")] = json.dumps([{"role": "user", "content": "ORD-008"}])
        self.assertEqual(await redis_client.get_history("s2", owner="U-103"), [])

    async def test_clear_session_scoped(self):
        await redis_client.append_turn("s3", "q", "a", owner="U-100")
        self.assertFalse(await redis_client.clear_session("s3", owner="U-103"))
        self.assertTrue(await redis_client.clear_session("s3", owner="U-100"))

    async def test_append_does_not_leak_into_other_user(self):
        await redis_client.append_turn("s4", "U-100 的问题", "ORD-001", owner="U-100")
        await redis_client.append_turn("s4", "U-103 的问题", "ORD-008", owner="U-103")
        own = await redis_client.get_history("s4", owner="U-103")
        self.assertEqual([m["content"] for m in own], ["U-103 的问题", "ORD-008"])


# ── 5. 会话历史裁剪：别人的订单号不能顺着 history 溜进上下文 ─────────
class TestHistoryScoping(unittest.TestCase):
    def test_foreign_history_is_dropped(self):
        history = [
            {"role": "user", "content": "帮我查 ORD-008"},
            {"role": "assistant", "content": "ORD-008 已发货，1388 元"},
            {"role": "user", "content": "你好"},
        ]
        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        kept, dropped = access.scope_history(history, principal)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["content"], "你好")
        self.assertEqual(len(dropped), 2)

    def test_own_history_is_kept(self):
        history = [
            {"role": "user", "content": "帮我查 ORD-001"},
            {"role": "assistant", "content": "ORD-001 已发货"},
        ]
        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        kept, dropped = access.scope_history(history, principal)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])

    def test_anonymous_history_with_ids_is_dropped(self):
        history = [{"role": "user", "content": "ORD-001"},
                   {"role": "user", "content": "你好"}]
        kept, _dropped = access.scope_history(history, identity.ANONYMOUS)
        self.assertEqual([m["content"] for m in kept], ["你好"])


# ── 6. 中间件：每个请求都真的鉴了一次 ─────────────────────────────
class _Recorder:
    """假的 ASGI 应用：只记录它看到的身份"""

    def __init__(self):
        self.principal = None
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.principal = identity.current_principal()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class TestIdentityMiddleware(unittest.IsolatedAsyncioTestCase):
    async def _call(self, headers):
        app = _Recorder()
        middleware = IdentityMiddleware(app)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/api/agent/stream",
                 "headers": headers}
        await middleware(scope, receive, send)
        return app, sent

    async def test_valid_token_sets_principal(self):
        token = identity.issue_token("U-103")
        app, sent = await self._call([(b"authorization", f"Bearer {token}".encode())])
        self.assertTrue(app.principal.authenticated)
        self.assertEqual(app.principal.user_id, "U-103")
        self.assertEqual(sent[0]["status"], 200)

    async def test_missing_token_is_anonymous(self):
        app, _sent = await self._call([])
        self.assertTrue(app.principal.anonymous)
        self.assertEqual(app.principal.reason, "no_token")

    async def test_tampered_token_is_anonymous(self):
        token = identity.issue_token("U-103")
        app, _sent = await self._call([(b"authorization", f"Bearer {token}x".encode())])
        self.assertTrue(app.principal.anonymous)
        self.assertEqual(app.principal.reason, "bad_signature")

    async def test_principal_does_not_leak_between_requests(self):
        token = identity.issue_token("U-103")
        await self._call([(b"authorization", f"Bearer {token}".encode())])
        self.assertTrue(identity.current_principal().anonymous)

    async def test_tools_cannot_be_reached_with_someone_elses_identity(self):
        """端到端的核心断言：伪造令牌 → 中间件给匿名 → 工具拒绝 → 拿不到订单"""
        app = _Recorder()
        middleware = IdentityMiddleware(app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        token = identity.issue_token("U-100")
        forged = token[:-3] + "zzz"
        scope = {"type": "http", "method": "POST", "path": "/api/agent/stream",
                 "headers": [(b"authorization", f"Bearer {forged}".encode())]}
        await middleware(scope, receive, send)

        payload = json.loads(get_order_info_tool.invoke({"orderId": "ORD-008"}))
        self.assertEqual(payload["code"], "UNAUTHENTICATED")
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
