"""订单链路的黄金边界测试：身份不可伪造 → 行级权限 → 历史裁剪 → 工具事实 → 出口接地

一个文件盯住"订单数据从入口到出口"的整条防线，只留线上真实踩过的坑与核心边界：

1. 令牌安全：身份只能由服务端签发（HMAC-SHA256），客户端改一个字节就作废，
   伪造令牌 → 中间件给匿名 → 工具 UNAUTHENTICATED（水平越权 / IDOR 回归）；
2. 行级权限：三条取数路径（getUserOrders / getOrderInfo / getLogisticsInfo）
   只能查本人，别人的数据是 FORBIDDEN 而不是"不存在"（不能顺带泄露"这个 ID 存不存在"）；
3. 历史裁剪：会话历史里别人的订单号不能顺着上下文溜进本次回答；
4. 工具与确定性查询：工具必须回真事实（把"账号没订单"和"ID 不存在"分开说）；
   省略写法「004」要能从上文补全，补不出唯一结果就不猜；
5. 出口接地：模型编造的订单号一律拦下换成安全话术，但已核实过的历史回答不能被误杀。

每条 test 的 docstring 都保留了原来的"线上现象 → 根因 → 修法"，那是这个项目的卖点。
"""
import asyncio
import json
import time
import unittest

from langchain_core.messages import AIMessage, HumanMessage

from app.security import access, identity
from app.security.middleware import IdentityMiddleware
from app.tools.order_tools import (
    deterministic_lookup,
    get_logistics_tool,
    get_order_info_tool,
    get_user_orders_tool,
    known_user_ids,
)
from app.utils.grounding import (
    UNGROUNDED_ANSWER,
    check_answer,
    facts_text,
    history_facts,
    sanitize,
    ungrounded_order_ids,
)

REAL_FACTS = (
    '[{"orderId": "ORD-005", "status": "已发货", "amount": 1599.0},'
    ' {"orderId": "ORD-007", "status": "已取消", "amount": 249.0}]'
)

# 省写补全用的上文：用户刚看完订单一览
SHORTHAND_HISTORY = [
    {"role": "user", "content": "查一下我的订单"},
    {"role": "assistant", "content": "您有两笔已发货订单：ORD-001 蓝牙耳机、ORD-010 机械键盘。"},
]

# 接地校验用的上文：这几笔是前几轮真实查出来的
GROUNDING_HISTORY = [
    {"role": "user", "content": "U-103"},
    {"role": "assistant", "content": "亲，U-103 名下有 ORD-008、ORD-009 两笔订单～"},
    {"role": "user", "content": "好的"},
]


def _json(payload):
    return payload if isinstance(payload, dict) else json.loads(payload)


class _RecorderApp:
    """假的 ASGI 应用：只记录它在本次请求里看到的身份"""

    def __init__(self):
        self.principal = None
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.principal = identity.current_principal()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _through_middleware(headers):
    """让一个请求真的走一遍 IdentityMiddleware，返回 (下游 app, 发给客户端的消息)"""
    app = _RecorderApp()
    middleware = IdentityMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/agent/stream", "headers": headers}
    await middleware(scope, receive, send)
    return app, sent


# ── 1. 令牌与行级权限（身份不可伪造 + 数据只能查本人）────────────────
class TestTokenAndRowScope(unittest.TestCase):
    def test_issued_token_verifies(self):
        """签发/验签链路的正对照：服务端签发的令牌换回身份，中间件每个请求都真的鉴了一次

        这条留作对照：没有"能过"的一侧，下面那些"拒绝"就分不清是拦对了还是全坏了。
        """
        token = identity.issue_token("U-103")
        principal, reason = identity.verify_token(token)
        self.assertEqual(reason, "ok")
        self.assertTrue(principal.authenticated)
        self.assertEqual(principal.user_id, "U-103")

        async def _scenario():
            app, sent = await _through_middleware([(b"authorization", f"Bearer {token}".encode())])
            # 请求处理完必须立刻清干净：否则下一个请求会顶着上一个人的身份跑
            return app, sent, identity.current_principal()

        app, sent, after_request = asyncio.run(_scenario())
        self.assertTrue(app.principal.authenticated)
        self.assertEqual(app.principal.user_id, "U-103")
        self.assertEqual(sent[0]["status"], 200)
        self.assertTrue(after_request.anonymous)

    def test_tampered_or_expired_token_is_rejected(self):
        """改一个字节就作废：伪造 sub / 坏签名 / 过期 / 格式错 / 账号被删 —— 一律 fail closed

        线上事故：改造前整条链路没有任何"我是谁"的凭证，前端在请求体里塞谁就是谁 ——
        把 user_id 改成 U-103 就能看 U-103 的订单（水平越权 / IDOR）。
        现在身份只认令牌签名里的 sub，验不过就退化成匿名。
        """
        token = identity.issue_token("U-100")
        prefix, _body, signature = token.split(".")
        forged_body = identity._b64e(json.dumps(
            {"sub": "U-103", "exp": int(time.time()) + 3600}, ensure_ascii=False).encode())
        principal, reason = identity.verify_token(f"{prefix}.{forged_body}.{signature}")
        self.assertIsNone(principal)
        self.assertEqual(reason, "bad_signature")

        broken = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
        self.assertEqual(identity.verify_token(broken), (None, "bad_signature"))

        self.assertEqual(identity.verify_token(identity.issue_token("U-100", ttl_seconds=-10)),
                         (None, "expired"))

        for bad in ("", "abc", "jisu1.xxx", "jisu1.xxx.yyy.zzz"):
            principal, _reason = identity.verify_token(bad)
            self.assertIsNone(principal, bad)

        # 签名对、但账号不在册（比如账号被删了）→ 不认；也不允许给不存在的账号签发
        body = identity._b64e(json.dumps(
            {"sub": "U-999", "exp": int(time.time()) + 3600}, ensure_ascii=False).encode())
        signed_unknown = f"{identity.TOKEN_PREFIX}.{body}.{identity._sign(body)}"
        self.assertEqual(identity.verify_token(signed_unknown), (None, "unknown_user"))
        with self.assertRaises(ValueError):
            identity.issue_token("U-999")

        # 端到端核心断言：伪造令牌 → 中间件给匿名 → 工具直接拒绝，拿不到订单
        forged = token[:-3] + "zzz"
        app, _sent = asyncio.run(_through_middleware(
            [(b"authorization", f"Bearer {forged}".encode())]))
        self.assertTrue(app.principal.anonymous)
        self.assertEqual(app.principal.reason, "bad_signature")
        payload = json.loads(get_order_info_tool.invoke({"orderId": "ORD-008"}))
        self.assertEqual(payload["code"], "UNAUTHENTICATED")

    def test_only_own_orders_are_returned(self):
        """只能查本人：别人的订单号/快递单号一律 FORBIDDEN，而且一个字段都不给

        三条取数路径缺一条就是越权漏洞；其中快递单号最隐蔽 ——
        绕过订单号直接报单号就能看别人物流。
        另外"别人的 ID"必须回 FORBIDDEN 而不是"不存在"：不能顺带告诉对方这个 ID 存不存在。
        """
        with identity.acting_as("U-100"):
            own = _json(get_user_orders_tool.invoke({"userId": "U-100"}))
        self.assertIn("ORD-001", [item["orderId"] for item in own])

        with identity.acting_as("U-100"):
            foreign_list = _json(get_user_orders_tool.invoke({"userId": "U-103"}))
        self.assertEqual(foreign_list["code"], "FORBIDDEN")
        self.assertIn("无权查看", foreign_list["error"])
        self.assertNotIn("不存在", foreign_list["error"])
        self.assertNotIn("ORD-008", json.dumps(foreign_list, ensure_ascii=False))

        with identity.acting_as("U-100"):
            foreign_order = _json(get_order_info_tool.invoke({"orderId": "ORD-008"}))
        self.assertEqual(foreign_order["code"], "FORBIDDEN")
        text = json.dumps(foreign_order, ensure_ascii=False)
        self.assertNotIn("已发货", text)
        self.assertNotIn("1388", text)
        self.assertNotIn("降噪头戴耳机", text)

        with identity.acting_as("U-100"):
            foreign_logistics = _json(get_logistics_tool.invoke({"trackingNo": "SF9988776655"}))
        self.assertEqual(foreign_logistics["code"], "FORBIDDEN")
        self.assertNotIn("records", foreign_logistics)

        # 同一单快递，本人查得到（证明拒绝不是因为"单号无效"）
        with identity.acting_as("U-103"):
            own_logistics = _json(get_logistics_tool.invoke({"trackingNo": "SF9988776655"}))
        self.assertIn("records", own_logistics)
        self.assertTrue(own_logistics["records"])

        # 模型没传 userId（用户只说"我有哪些订单"）→ 默认查本人
        with identity.acting_as("U-101"):
            default_self = _json(get_user_orders_tool.invoke({}))
        ids = [item["orderId"] for item in default_self]
        self.assertIn("ORD-003", ids)
        self.assertNotIn("ORD-001", ids)  # ORD-001 是 U-100 的

    def test_anonymous_gets_nothing(self):
        """没登录（没选身份 / 令牌过期 / 验签失败）：三个工具全部拒绝，且不带任何数据

        fail closed：宁可什么都不给，也不能让"没有身份"的请求拿到订单。
        """
        for call in (lambda: get_user_orders_tool.invoke({"userId": "U-100"}),
                     lambda: get_order_info_tool.invoke({"orderId": "ORD-001"}),
                     lambda: get_logistics_tool.invoke({"trackingNo": "SF1234567890"})):
            payload = _json(call())
            self.assertEqual(payload["code"], "UNAUTHENTICATED")
            self.assertNotIn("ORD-001", json.dumps(payload, ensure_ascii=False))

        # 中间件没带令牌 → 匿名，且原因可查（前端据此提示"先选身份"）
        app, _sent = asyncio.run(_through_middleware([]))
        self.assertTrue(app.principal.anonymous)
        self.assertEqual(app.principal.reason, "no_token")

        # 绕开模型的确定性查询同样 fail closed
        result = deterministic_lookup("ORD-001")
        self.assertIn("未登录", result["answer"])

    def test_claimed_id_cannot_change_scope_and_is_audited(self):
        """令牌是 U-100、入参写 U-103：入参只是"声称"，永远不参与鉴权 —— 拒绝并且要留痕

        攻击路径：提示词注入 / 改前端请求体让工具换个人 ID 去查。
        鉴权只看令牌里的 sub；声称与令牌不符就记一条 identity_spoof 供审计。
        """
        with identity.acting_as("U-100"):
            payload = _json(get_user_orders_tool.invoke({"userId": "U-103"}))
        self.assertEqual(payload["code"], "FORBIDDEN")

        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        self.assertTrue(identity.audit_claim(principal, "U-103", route="agent"))
        events = [e for e in identity.recent_incidents(limit=10) if e["kind"] == "identity_spoof"]
        self.assertTrue(events)
        self.assertEqual(events[0]["claimed"], "U-103")
        # 声称与令牌一致（哪怕大小写写法不同）不算事故，否则审计会被正常请求刷屏
        self.assertFalse(identity.audit_claim(principal, "u-100", route="agent"))

        # 越权尝试本身也要留得下痕：谁、想查什么
        with identity.acting_as("U-100"):
            get_order_info_tool.invoke({"orderId": "ORD-008"})
        denied = [e for e in identity.recent_incidents(limit=10)
                  if e["kind"] == "order_scope_denied"]
        self.assertTrue(denied)
        self.assertEqual(denied[0]["actor"], "U-100")
        self.assertEqual(denied[0]["target"], "ORD-008")


# ── 2. 会话历史裁剪：别人的订单号不能顺着 history 溜进上下文 ─────────
class TestHistoryScoping(unittest.TestCase):
    def test_foreign_history_is_dropped(self):
        """历史里引用他人数据的消息必须裁掉

        场景：用户 A 查完订单，会话缓存 / 前端 localStorage 里留着 A 的订单号；
        换用户 B 进来（或 B 伪造一份 history），模型会把 A 的订单当"已核实的事实"复述出去。
        入口处裁掉，比指望模型自觉可靠。
        """
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

        # 匿名更严：带订单号的历史一条都不留（不是"别人的"不许看，而是谁都无权）
        anon_kept, _dropped = access.scope_history(
            [{"role": "user", "content": "ORD-001"}, {"role": "user", "content": "你好"}],
            identity.ANONYMOUS)
        self.assertEqual([m["content"] for m in anon_kept], ["你好"])

    def test_own_history_is_kept(self):
        """本人订单号不能被误裁：裁过头会直接把多轮对话的上下文弄丢"""
        history = [
            {"role": "user", "content": "帮我查 ORD-001"},
            {"role": "assistant", "content": "ORD-001 已发货"},
        ]
        principal = identity.Principal(user_id="U-100", user_name="张伟",
                                       authenticated=True, reason="ok")
        kept, dropped = access.scope_history(history, principal)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])


# ── 3. 订单工具与确定性查询：真事实 + 省写补全 + 不猜 ────────────────
class TestOrderToolsAndLookup(unittest.TestCase):
    def test_tool_output_is_real_facts(self):
        """工具必须回真事实，并且把「账号存在但没订单」和「ID 不存在」分开说

        线上事故：用户报一个不存在的 ID，工具含糊成"暂无订单"，
        模型只能猜"可能没下过单，或者 ID 有误"，用户得不到确定答案。
        """
        with identity.acting_as("U-102"):
            payload = json.loads(get_user_orders_tool.invoke({"userId": "U-102"}))
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)

        with identity.acting_as("U-104"):
            empty = json.loads(get_user_orders_tool.invoke({"userId": "U-104"}))
        self.assertIn("名下暂无订单", empty["error"])
        self.assertNotIn("不存在", empty["error"])

        with identity.acting_as("U-100"):
            missing_order = json.loads(get_order_info_tool.invoke({"orderId": "ORD-999"}))
        self.assertIn("不存在", missing_order["error"])
        self.assertIn("hint", missing_order)

        with identity.acting_as("U-100"):
            missing_tracking = json.loads(get_logistics_tool.invoke({"trackingNo": "SF0000000000"}))
        self.assertIn("不存在", missing_tracking["error"])

        # 用户输入小写 / 带空格的 ID 也要能查到
        with identity.acting_as("U-102"):
            normalized = json.loads(get_user_orders_tool.invoke({"userId": " u-102 "}))
        self.assertIsInstance(normalized, list)

        # 存在性以账号表为准而不是订单表：U-104 有账号没订单，同样"存在"
        self.assertIn("U-100", known_user_ids())
        self.assertIn("U-104", known_user_ids())
        self.assertTrue(all(uid.startswith("U-") for uid in known_user_ids()))

        # 模型没调工具时的兜底查询：answer 就是工具原始事实（线上编造订单表的事故防线）
        with identity.acting_as("U-102"):
            facts = deterministic_lookup("ORD-005")
        self.assertIn("ORD-005", facts["answer"])
        self.assertIn("已发货", facts["answer"])

        multi = deterministic_lookup("帮我查 ORD-001，另外 U-102 有哪些订单")
        tools = [step["tool"] for step in multi["steps"]]
        self.assertIn("getOrderInfo", tools)
        self.assertIn("getUserOrders", tools)

        # 抠不出 ID 就没法确定性查询，只能引导用户提供 ID
        self.assertIsNone(deterministic_lookup("你好呀"))
        self.assertIsNone(deterministic_lookup("我有哪些订单"))

    def test_shorthand_order_ids_resolved_from_context(self):
        """省略写法 / 指代要能落到具体订单上

        线上实测：用户看完订单一览后追问「004为什么没有下单时间」。
        "004" 抠不出 ORD 号 → 模型凭上文记忆作答 → 出口接地校验判 ORD-004 不在事实里 →
        整段被换成"这笔数据没能核实到"（而工具其实查得到，四笔订单每笔都带 createTime）。
        """
        with identity.acting_as("U-100"):
            result = deterministic_lookup("004为什么没有下单时间", history=SHORTHAND_HISTORY)
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getOrderInfo")
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-004")
        self.assertIn("2025-03-15", result["answer"])   # createTime 真的带回来了

        with identity.acting_as("U-100"):
            nth = deterministic_lookup("第二笔到哪了", history=SHORTHAND_HISTORY)
        self.assertEqual(nth["steps"][0]["input"]["orderId"], "ORD-010")

        with identity.acting_as("U-100"):
            anaphora = deterministic_lookup("这单发货了吗", history=SHORTHAND_HISTORY)
        self.assertEqual(anaphora["steps"][0]["input"]["orderId"], "ORD-010")

        # 新会话直接问 "004"：用本人名下的订单号补全（只认自己的，不猜别人的）
        with identity.acting_as("U-100"):
            fresh = deterministic_lookup("004 到哪了")
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["steps"][0]["input"]["orderId"], "ORD-004")

        # 线上用户会发 "u103给我看一下物流"（小写、漏连字符、中文紧跟着）
        with identity.acting_as("U-103"):
            dashless_user = deterministic_lookup("u103给我看一下物流")
        self.assertIsNotNone(dashless_user)
        self.assertEqual(dashless_user["steps"][0]["tool"], "getUserOrders")
        self.assertEqual(dashless_user["steps"][0]["input"]["userId"], "U-103")
        self.assertIn("ORD-008", dashless_user["answer"])

        # ORD-006 是 U-101 的订单，顺便验证"查自己的单"能过
        with identity.acting_as("U-101"):
            dashless_order = deterministic_lookup("ord006 到哪了")
        self.assertEqual(dashless_order["steps"][0]["tool"], "getOrderInfo")
        self.assertEqual(dashless_order["steps"][0]["input"]["orderId"], "ORD-006")
        self.assertNotIn("不存在", dashless_order["answer"])

        # 写全的订单号优先，不能被"省写补全"改写
        with identity.acting_as("U-100"):
            full_id = deterministic_lookup("ORD-004 详情", history=SHORTHAND_HISTORY)
        self.assertEqual(full_id["steps"][0]["input"]["orderId"], "ORD-004")

    def test_ambiguous_or_foreign_shorthand_is_not_guessed(self):
        """补不出唯一结果就不猜：猜错等于把别人的订单当成用户的报出去

        ① 一句话里两个省写号；② 别人的省写号；③ 金额不是订单号。
        另外兜底查询同样受权限约束 —— 绕开模型不等于绕开权限。
        """
        # "001 和 011 哪个先到"：猜哪个都不对
        history = [{"role": "assistant", "content": "ORD-001 已发货；ORD-011 待发货"}]
        with identity.acting_as("U-100"):
            self.assertIsNone(deterministic_lookup("001 和 011 哪个先到", history=history))

        # U-102 名下没有 ORD-001 附近的号：补不出来，也不拿别人的号去试
        with identity.acting_as("U-102"):
            self.assertIsNone(deterministic_lookup("001为什么没有下单时间"))

        # 金额/数量这类数字不能被当成订单号后缀（138.9 元不是 ORD-138）
        with identity.acting_as("U-100"):
            self.assertIsNone(deterministic_lookup("为什么扣了我 138 元", history=SHORTHAND_HISTORY))

        # 别人报了一个不属于自己的用户 ID：兜底查询也只回"无权查看"，不漏数据
        result = deterministic_lookup("U-108 有哪些订单", identity.Principal(
            user_id="U-102", user_name="王强", authenticated=True, reason="ok"))
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getUserOrders")
        self.assertIn("无权查看", result["answer"])


# ── 4. 出口接地校验：编造要拦、真实要放 ────────────────────────────
class TestGrounding(unittest.TestCase):
    def test_fabricated_order_ids_are_blocked(self):
        """线上真实事故：用户发 "U-108"，Agent 这轮没调工具，直接编了一张订单表
        （ORD-003 已发货 299 元 智能手环 B5 —— 真实数据里 ORD-003 是 U-101 的，也根本没有 U-108）

        提示词里写了"不许编造"，模型仍可能绕过工具直接生成，所以出口再做一道结构性校验：
        回答里的订单号必须在工具事实里找得到，否则整段换成安全话术。
        """
        fabricated = (
            "亲，帮您查到用户 U-108 的订单列表如下：\n"
            "| ORD-003 | 已发货 | 299.0 元 | 智能手环 B5 |\n"
            "| ORD-005 | 已取消 | 599.0 元 | 便携蓝牙音箱 S3 |"
        )
        ok, detail = check_answer(fabricated, REAL_FACTS)
        self.assertFalse(ok)
        self.assertIn("ORD-003", detail["offending"])      # 事实里没有
        self.assertNotIn("ORD-005", detail["offending"])   # 事实里有，虽然内容是编的

        # 工具明确报错（用户不存在）时，回答里出现的订单号全都是编的
        ok, detail = check_answer("亲，帮您查到 ORD-003、ORD-006 两笔订单～",
                                  '{"error": "用户 U-108 不存在"}')
        self.assertFalse(ok)
        self.assertEqual(len(detail["offending"]), 2)

        # 挂着"例如"的幌子但后面跟着数据 —— 仍然是编造，照样拦
        ok, detail = check_answer("例如 ORD-003 已发货，金额 299 元，智能手环 B5", "无")
        self.assertFalse(ok)
        self.assertEqual(detail["offending"], ["ORD-003"])

        # "如果 ORD-003 …" 里的「如」不是示例提示词（曾被误当示例、整句放行）
        ok, detail = check_answer("如果 ORD-003 还没有发货，请耐心等待", "无")
        self.assertFalse(ok)
        self.assertEqual(detail["offending"], ["ORD-003"])

        # 连数据都没有的裸订单号，也照拦
        ok, detail = check_answer("亲，您还有一笔 ORD-777 哦～", "无")
        self.assertFalse(ok)
        self.assertEqual(detail["offending"], ["ORD-777"])

        # 大小写不敏感：小写 ord-999 一样要认出来
        self.assertEqual(ungrounded_order_ids("ord-999", "ORD-001"), ["ORD-999"])

        # 拦下后替换成安全话术，并留下事故详情（route 便于定位是哪条链路）
        answer, incident = sanitize("查到 ORD-999 了", REAL_FACTS, route="graph")
        self.assertEqual(answer, UNGROUNDED_ANSWER)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["route"], "graph")

    def test_grounded_answer_passes(self):
        """放行对照：事实里的订单号、以及压根不提订单号的回答，都不能被误杀

        线上实测误杀：用户发一句看不懂的话，模型热心列举"订单号格式如 ORD-001"，
        整段被判成编造、换成"没能核实到"，用户一脸懵。
        """
        ok, _detail = check_answer("亲，U-102 名下有 ORD-005（已发货）和 ORD-007（已取消）～",
                                   REAL_FACTS)
        self.assertTrue(ok)

        ok, _detail = check_answer("亲，没有查到该用户的订单哦～", REAL_FACTS)
        self.assertTrue(ok)

        example = (
            "亲，您好呀～您方便再说一下想咨询什么吗？比如：\n"
            "- 查询订单状态、订单内容（需要提供订单号，格式如 ORD-001）\n"
            "- 查询您名下的订单列表（需要提供用户 ID，格式如 U-100）"
        )
        ok, _detail = check_answer(example, "无")
        self.assertTrue(ok, "格式示例不该被当成编造订单")

    def test_verified_history_counts_as_facts(self):
        """已经核实过的历史回答也要算事实来源，否则真实数据会被误判成编造

        线上事故（Agent 页实测）：用户查完 U-103 的订单（ORD-008 / ORD-009）后只补一句 "U-103"，
        模型这一轮没调工具、直接沿用上文作答；出口校验只比对本轮工具事实，
        于是把 ORD-008 判成编造，整段换成"没能核实到" —— 数据明明是前几轮真实查出来的。
        这些历史回答在产出当时都过了同一道校验，可以安全地当事实来源；
        真正**新**编造的订单号依然拦得住。
        """
        answer = "亲，您刚才那两笔是 ORD-008 和 ORD-009 哦～"
        self.assertFalse(check_answer(answer, "无")[0])   # 只看本轮事实 → 误判成编造

        facts = facts_text("无", history_facts(GROUNDING_HISTORY))
        self.assertTrue(check_answer(answer, facts)[0])   # 并入历史回答 → 通过

        ok, detail = check_answer("亲，还有一笔 ORD-777 也是您的～", facts)
        self.assertFalse(ok)
        self.assertEqual(detail["offending"], ["ORD-777"])

        # graph 链路传进来的是 LangChain 消息对象而不是 dict；且只有"客服说过的"才算事实
        history = [AIMessage(content="查到 ORD-005 已发货"), HumanMessage(content="谢谢")]
        messages_facts = history_facts(history)
        self.assertIn("ORD-005", messages_facts)
        self.assertNotIn("谢谢", messages_facts)


if __name__ == "__main__":
    unittest.main()
