"""退款族「判定分层 + 人工接力」的黄金边界测试（纯逻辑，不联网）

这个文件只盯住一件事：**用户说要办的时候别再问一圈，用户只是问问的时候别再甩人工电话**。
它合并了原先 tests/test_refund_handoff.py 的 19 条用例，只留 5 条黄金边界，
每条都对应一次线上真实事故、一处容易改错的分层三态逻辑，或者一处安全层放行对照。

两次线上真实反馈（都是被真实用户打脸的，原样保留）：
    1. "我说退款，它问我一大堆，最后告诉我办不了，气死我了。"
       —— 系统没有退款分支：意图识别把退款归到 order，订单 Agent 就追问订单号，
          问完工具只会查不会办，最后回一句"办不了"。先追问、后拒绝。
    2. "退款需要多少天？" 被回了一句"请联系人工"。
       —— 用关键词表判"是办还是问"，词表没写"多少天"，就把咨询当成了要办。

判定分层（app/utils/handoff.py）：
    L0 规则快路径  零 token：明显要办 → action；整句无关 → none；灰区交下两层
    L1 缓存        同样的输入不再花第二次 token
    L2 小模型      灰区才调，输出 action / info / progress / mixed / none
    L3 失败兜底    fail-open：模型超时、报错、熔断一律走正常链路，绝不堵死

本文件的 5 条：
    1. 明显要办 → 人工通道，话术带热线、不反问任何信息
    2. 咨询（"退款需要多少天？"）→ 绝不甩人工电话，走知识库
    3. 查"我这一单退款进度" → 报了单号走订单工具，没报的也不许反问单号
    4. 判定层放行边界 → mixed / none 放行、模型失败与熔断 fail-open、同一句只花一次 token
    5. 话术与知识库口径一致（热线、服务时段、到账时效），防止两边跑偏
"""
import asyncio
import os
import unittest
from unittest import mock

from app.resilience import get_breaker
from app.security import rules
from app.utils import handoff


def classify(text: str) -> dict:
    return asyncio.run(handoff.classify(text))


def stub_judge(kind: str, **extra):
    """把『小模型』换成本地桩：单测不联网、不花钱，还能断言到底有没有被调用"""
    async def _fake(_text):
        return {"kind": kind, "layer": "model", "ok": True, "model": "stub", "model_tokens": 7,
                "reason": "桩判定为 %s" % kind, **extra}
    return mock.MagicMock(side_effect=_fake)


class TestRefundHandoff(unittest.TestCase):
    """共用夹具：每个用例都从干净的判定缓存开始，并且**不碰 Redis**

    两个原因（都是真实踩过的）：
      1. 用例之间互不影响：上一条用例、甚至上一次真实请求留下的缓存，不该顶掉这次的结果；
      2. 单测不该依赖 Redis：本机没起 Redis 时，每次写缓存都要等 1s 连接超时，
         整套用例会从 0.4s 变成几分钟。
    """

    def setUp(self):
        handoff._memory_cache.clear()
        self._cache_patch = mock.patch.object(handoff, "_cache_get", return_value=None)
        self._cache_set_patch = mock.patch.object(handoff, "_cache_set",
                                                  mock.MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0)))
        self._cache_patch.start()
        self._cache_set_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._cache_set_patch.stop()

    # ── 1. 明显要办 ──────────────────────────────────────────────
    def test_obvious_action_goes_to_human_with_hotline_and_no_questions(self):
        """明显要办的事：安全层白名单放行 → 规则快路径零 token → 四入口直接给人工通道

        线上事故 1 的回归：用户说"我要退款"，系统先追问订单号、再说"办不了"。
        这条同时盯住四件事：一次小模型都不调（省钱且快）、话术里带可点拨号的热线、
        不问任何信息（不许出现问号）、手机上一屏读得完。
        """
        obvious = ["我要退款", "帮我退款 ORD-003", "我要申请退货",
                   "我要投诉你们", "帮我把收货地址改一下"]
        for text in obvious:
            with self.subTest(text=text):
                # 第一道：安全层业务白名单（命中业务词 + 没有可疑词）零 token 放行
                self.assertTrue(rules.is_business(rules.normalize(text)))
                self.assertTrue(rules.whitelisted(rules.normalize(text)))
                # 第二道：规则快路径直判 action，桩成 info 也照样不该被调用
                with mock.patch.object(handoff, "_judge", stub_judge("info")) as fake:
                    verdict = classify(text)
                self.assertEqual(verdict["kind"], "action")
                self.assertEqual(verdict["layer"], "rule")
                fake.assert_not_called()

        # 用户顺口报了单号：要带上（人工接通直接报号最快），而不是反问他还要什么
        verdict = classify("帮我退款 ORD-003")
        self.assertEqual(verdict["order_id"], "ORD-003")
        self.assertIn("ORD-003", handoff.action_reply(verdict))

        # 四个入口都要给到人工通道，且不许改走知识库
        verdict = classify("我要退款")
        for page in ("chat", "agent", "rag", "graph"):
            with self.subTest(page=page):
                plan = handoff.decide(verdict, page)
                self.assertIn(handoff.HOTLINE, plan["reply"])
                self.assertIn("tel:%s" % handoff.HOTLINE, plan["reply"])
                self.assertFalse(plan["kb"])

        # 话术质量：不反问订单号 / 用户 ID、不出现任何问号、一屏读得完
        for text in ["我要退款", "我要投诉", "帮我把收货地址改一下"]:
            verdict = classify(text)
            with self.subTest(text=text):
                for script in (handoff.action_reply(verdict), handoff.chat_reply(verdict)):
                    self.assertNotRegex(
                        script,
                        r"(请|麻烦|需要)(您)?(提供|告诉|发|报)(一下)?[^。\n]{0,8}(订单号|用户\s*ID)")
                    self.assertNotIn("？", script)
                    self.assertNotIn("?", script)
        self.assertLess(len(handoff.action_reply(classify("我要退款"))), 420,
                        "话术太长，手机上一屏看不完")

    # ── 2. 只是咨询 ──────────────────────────────────────────────
    def test_asking_about_refund_never_gets_the_human_line(self):
        """咨询类（"退款需要多少天？"）不许走办理快路径，更不许被推去人工

        线上事故 2 的回归：词表没写"多少天"，把咨询当成了要办，回了"请联系人工"。
        现在这些句子必须落到小模型判 info → 走知识库；答案由知识库原文给出，
        本模块一个字都不许硬答，也不许在回答后自动补一句人工电话。
        """
        asking = ["退款需要多少天？", "钢化膜能退吗", "退款收到货后几天退钱",
                  "退款政策是什么", "我的退款到哪了", "我买的耳机不想要了能退吗"]
        for text in asking:
            with self.subTest(text=text):
                self.assertTrue(rules.whitelisted(rules.normalize(text)))
                with mock.patch.object(handoff, "_judge", stub_judge("info")) as fake:
                    verdict = classify(text)
                self.assertNotEqual(verdict["layer"], "rule", "被规则快路径误判成办理：%s" % text)
                self.assertEqual(verdict["kind"], "info")
                fake.assert_called_once()

        with mock.patch.object(handoff, "_judge", stub_judge("info")):
            verdict = classify("退款需要多少天？")
        self.assertEqual(verdict["layer"], "model", "咨询句不该在规则层就被定性")
        plan = handoff.decide(verdict, "graph")
        self.assertIsNone(plan["reply"], "咨询类问题不该由本模块写死回答")
        self.assertTrue(plan["kb"])
        self.assertEqual(plan["preset_intents"], ["knowledge"], "落地页应直接去知识库")
        self.assertTrue(handoff.decide(verdict, "chat")["kb"])
        self.assertTrue(handoff.decide(verdict, "agent")["kb"])
        self.assertIsNone(handoff.decide(verdict, "rag")["preset_intents"])
        # 四个入口都不许出现人工电话（只是在问，不该被"推脱"）
        for page in ("chat", "agent", "rag", "graph"):
            with self.subTest(page=page):
                self.assertNotIn(handoff.HOTLINE, handoff.decide(verdict, page)["reply"] or "")
        self.assertFalse(hasattr(handoff, "policy_footer"), "不该再有『回答后自动补人工』的函数")

    # ── 3. 查这一单的进度 ────────────────────────────────────────
    def test_refund_progress_goes_to_the_order_tool_never_interrogates(self):
        """问"我这一单"的进度：报了单号直接查订单工具，没报单号的也不许反问他

        线上实测踩过（跟事故 1 同一种体验）：用户只想问进度，系统反问"请提供订单号"，
        问完还只能查不能办。所以分成两种：报了订单号 / 用户 ID → 订单工具给真实状态；
        什么都没报 → 用知识库回答"进度在哪儿看"，绝不反问。
        """
        with mock.patch.object(handoff, "_judge", stub_judge("progress")):
            with_id = classify("退款进度查一下 ORD-006")
        plan = handoff.decide(with_id, "graph")
        self.assertEqual(plan["preset_intents"], ["order"], "报了单号就该去订单工具查真实状态")
        self.assertIsNone(plan["reply"])
        self.assertFalse(plan["kb"])
        self.assertIsNone(handoff.decide(with_id, "agent")["reply"])
        self.assertIn("[[go:agent]]", handoff.decide(with_id, "chat")["reply"])

        with mock.patch.object(handoff, "_judge", stub_judge("progress")):
            no_id = classify("昨天申请的退款还没到")
        self.assertEqual(handoff.decide(no_id, "graph")["preset_intents"], ["knowledge"])
        self.assertIsNone(handoff.decide(no_id, "graph")["reply"])
        self.assertTrue(handoff.decide(no_id, "agent")["kb"])
        self.assertIsNone(handoff.decide(no_id, "rag")["preset_intents"])
        chat_script = handoff.decide(no_id, "chat")["reply"]
        self.assertIn("[[go:agent]]", chat_script)
        self.assertNotIn("？", chat_script)
        self.assertNotIn("?", chat_script)

    # ── 4. 判定层的放行边界 ──────────────────────────────────────
    def test_judge_layer_fails_open_and_costs_one_call_per_text(self):
        """灰区兜底层是"放行层"：判不出来、判挂了、熔断了，都不许把用户堵在门口

        四种边界：
          · 整句与退款无关 → 零 token 判 none，连小模型都不调；
          · 小模型判成 mixed / none（多意图 / 无关）→ 四个入口原样放行；
          · 小模型超时、报错、熔断打开 → fail-open，走正常链路；
          · 同一句话第二次命中缓存 → 不再花 token，也不用再等模型。
        """
        for text in ["耳机多少钱", "帮我查一下 U-101 的订单", "我的货怎么还没到", "你好呀"]:
            with self.subTest(text=text):
                with mock.patch.object(handoff, "_judge", stub_judge("action")) as fake:
                    verdict = classify(text)
                self.assertEqual(verdict["kind"], "none")
                self.assertEqual(verdict["layer"], "rule")
                fake.assert_not_called()

        # 小模型判出来的四种结果都要如实落到 kind / layer（不许被吞成默认值）
        for kind in ("info", "progress", "mixed", "none"):
            with self.subTest(kind=kind):
                with mock.patch.object(handoff, "_judge", stub_judge(kind)):
                    verdict = classify("退款这事我想了解下")
                self.assertEqual(verdict["kind"], kind)
                self.assertEqual(verdict["layer"], "model")

        # mixed / none：多意图或无关，一个入口都不许拦
        for kind in ("mixed", "none"):
            with self.subTest(kind=kind):
                with mock.patch.object(handoff, "_judge", stub_judge(kind)):
                    verdict = classify("退款顺带问下耳机多少钱")
                for page in ("chat", "agent", "rag", "graph"):
                    plan = handoff.decide(verdict, page)
                    self.assertIsNone(plan["reply"])
                    self.assertFalse(plan["kb"])
                    self.assertIsNone(plan["preset_intents"])

        # 模型超时 / 报错：fail-open（layer=error，kind=mixed，四个入口全部放行）
        async def boom(_text):
            return {"kind": "mixed", "layer": "error", "ok": False,
                    "reason": "小模型不可用：超时（fail-open：走正常链路）"}

        with mock.patch.object(handoff, "_judge", boom):
            verdict = classify("退款这事我想了解下")
        self.assertEqual(verdict["kind"], "mixed")
        self.assertEqual(verdict["layer"], "error")
        for page in ("chat", "agent", "rag", "graph"):
            with self.subTest(page=page):
                plan = handoff.decide(verdict, page)
                self.assertIsNone(plan["reply"])
                self.assertFalse(plan["kb"])

        # 熔断打开：连"有没有 Key、会不会超时"都不必等，判定层直接 fail-open
        breaker = get_breaker(handoff.JUDGE_BREAKER)
        breaker.reset()
        try:
            for _ in range(10):
                breaker.record_failure(RuntimeError("桩故障：判定小模型整体挂了"))
            if breaker.allow()[0]:
                self.skipTest("熔断器处于关闭 / dry-run 状态（环境配置如此），跳过熔断分支")
            verdict = classify("退款这事我想了解下")
            self.assertEqual(verdict["layer"], "circuit_open")
            self.assertEqual(verdict["kind"], "mixed")
            for page in ("chat", "agent", "rag", "graph"):
                with self.subTest(page=page):
                    plan = handoff.decide(verdict, page)
                    self.assertIsNone(plan["reply"])
                    self.assertFalse(plan["kb"])
        finally:
            breaker.reset()

        # 缓存：同一句话第二次不再调小模型（用户复述一遍不该再花一次 token、再等一次超时）
        store: dict = {}

        async def fake_get(text):
            return store.get(handoff._digest(text))

        async def fake_set(text, value):
            store[handoff._digest(text)] = value

        self._cache_patch.stop()
        self._cache_set_patch.stop()
        try:
            with mock.patch.object(handoff, "_cache_get", fake_get), \
                 mock.patch.object(handoff, "_cache_set", fake_set):
                with mock.patch.object(handoff, "_judge", stub_judge("info")) as fake:
                    classify("退款缓存专用句子-内存版")
                    verdict = classify("退款缓存专用句子-内存版")
            self.assertEqual(fake.call_count, 1, "第二次应当命中缓存，不再调用小模型")
            self.assertEqual(verdict["layer"], "cache")
        finally:
            self._cache_patch.start()
            self._cache_set_patch.start()

    # ── 5. 口径一致性 ────────────────────────────────────────────
    def _knowledge(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", "app", "data", "knowledge", "policies.md")
        with open(os.path.abspath(path), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_scripts_and_knowledge_base_agree_on_hotline_and_timing(self):
        """话术与知识库必须同口径：热线号码、服务时段、到账时效

        两边跑偏最难发现：话术里印一个号、知识库里写另一个号，用户按哪边打都可能打不通。
        所以热线与时段以知识库为准，由这条用头顶着；到账时效（3 个工作日 /
        3-5 个银行工作日）必须留在知识库里由检索给出，不许搬进代码写死。
        """
        content = self._knowledge()
        self.assertIn(handoff.HOTLINE, content,
                      "话术里的电话和知识库不一致：%s" % handoff.HOTLINE)
        self.assertIn(handoff.HOTLINE_HOURS, content,
                      "话术里的服务时段和知识库不一致：%s" % handoff.HOTLINE_HOURS)
        for fact in ("3 个工作日", "3-5 个银行工作日"):
            with self.subTest(fact=fact):
                self.assertIn(fact, content)
        # 话术本身也要把热线时段印出来，且与知识库是同一个字符串
        self.assertIn(handoff.HOTLINE_HOURS, handoff.action_reply(classify("我要退款")))


if __name__ == "__main__":
    unittest.main()
