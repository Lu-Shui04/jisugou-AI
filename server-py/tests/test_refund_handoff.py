"""退款族「意图判定 + 人工接力」的单测（纯逻辑，不联网）

两次线上真实反馈（都是被真实用户打脸的）：
    1. "我说退款，它问我一大堆，最后告诉我办不了，气死我了。"
    2. "退款需要多少天？" 被回了一句"请联系人工" —— 用关键词表判"是办还是问"，
       词表没写"多少天"，就把咨询当成了要办。

对应的验收标准：
1. **明显要办**的走零 token 快路径（不调小模型），直接给人工通道；
2. **咨询 / 进度 / 混合 / 无关**一律不拦 —— 咨询走知识库、进度走订单工具、其余原样放行；
3. 灰区交给小模型；模型不可用时 **fail-open**（走正常链路，绝不堵死）；
4. 话术本身不能二次激怒：给电话（可点拨号）、不许反问订单号、不许出现问号。
"""
import asyncio
import os
import unittest
from unittest import mock

from app.security import rules
from app.utils import handoff


def classify(text: str) -> dict:
    return asyncio.run(handoff.classify(text))


class JudgeTestCase(unittest.TestCase):
    """每个用例都从干净的判定缓存开始（否则上一条用例的结果会被缓存命中）"""

    def setUp(self):
        handoff._memory_cache.clear()
        # 默认把"判定缓存"整个关掉，两个原因：
        #   1. 用例之间互不影响（上一条用例甚至上一次真实请求留下的缓存不该顶掉这次的结果）
        #   2. 单测不该依赖 Redis —— 本机没起 Redis 时，每次写缓存都要等 1s 连接超时，
        #      整套用例会从 0.4s 变成几分钟（真实踩过）
        self._cache_patch = mock.patch.object(handoff, "_cache_get", return_value=None)
        self._cache_set_patch = mock.patch.object(handoff, "_cache_set",
                                                  mock.MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0)))
        self._cache_patch.start()
        self._cache_set_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._cache_set_patch.stop()


def stub_judge(kind: str, **extra):
    """把『小模型』换成本地桩：单测不联网、不花钱，还能断言到底有没有被调用"""
    async def _fake(_text):
        return {"kind": kind, "layer": "model", "ok": True, "model": "stub", "model_tokens": 7,
                "reason": "桩判定为 %s" % kind, **extra}
    return mock.MagicMock(side_effect=_fake)


class TestRuleFastPath(JudgeTestCase):
    """零 token 快路径：明显要办 / 明显无关，都不该浪费一次模型调用"""

    def test_obvious_action_uses_no_model(self):
        for text in ["我要退款", "帮我退款 ORD-003", "我要申请退货", "我要投诉你们",
                     "帮我把收货地址改一下"]:
            with self.subTest(text=text):
                with mock.patch.object(handoff, "_judge", stub_judge("info")) as fake:
                    verdict = classify(text)
                self.assertEqual(verdict["kind"], "action")
                self.assertEqual(verdict["layer"], "rule")
                fake.assert_not_called()

    def test_irrelevant_uses_no_model(self):
        for text in ["耳机多少钱", "帮我查一下 U-101 的订单", "我的货怎么还没到", "你好呀"]:
            with self.subTest(text=text):
                with mock.patch.object(handoff, "_judge", stub_judge("action")) as fake:
                    verdict = classify(text)
                self.assertEqual(verdict["kind"], "none")
                fake.assert_not_called()

    def test_asking_phrases_never_shortcut(self):
        """线上回归：这些是"问"，不能走零 token 的办理快路径"""
        for text in ["退款需要多少天？", "钢化膜能退吗", "退款收到货后几天退钱",
                     "退款政策是什么", "我的退款到哪了", "我买的耳机不想要了能退吗"]:
            with self.subTest(text=text):
                with mock.patch.object(handoff, "_judge", stub_judge("info")) as fake:
                    verdict = classify(text)
                self.assertNotEqual(verdict["layer"], "rule", "被规则快路径误判成办理：%s" % text)
                self.assertEqual(verdict["kind"], "info")
                fake.assert_called_once()

    def test_order_id_is_carried(self):
        verdict = classify("帮我退款 ORD-003")
        self.assertEqual(verdict["order_id"], "ORD-003")
        self.assertIn("ORD-003", handoff.action_reply(verdict))


class TestModelJudgement(JudgeTestCase):
    """小模型判定层：灰区交给它，失败 fail-open"""

    def test_model_kinds(self):
        for kind in ("info", "progress", "mixed", "none"):
            with self.subTest(kind=kind):
                with mock.patch.object(handoff, "_judge", stub_judge(kind)):
                    verdict = classify("退款这事我想了解下")
                self.assertEqual(verdict["kind"], kind)
                self.assertEqual(verdict["layer"], "model")

    def test_model_failure_fails_open(self):
        """模型超时 / 报错：走正常链路，绝不堵死"""
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

    def test_cache_avoids_second_call(self):
        """同样的输入只花一次小模型 token（这条要真的走缓存，所以临时打开）"""
        # 用一块内存假缓存验证"第二次不再调小模型"的逻辑：
        # 单测不依赖 Redis（本机没起 Redis 时，真连一次要等 1s 超时，整套用例会慢 40 倍）
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


class TestRouting(JudgeTestCase):
    """判定结果只决定走哪条路：actions 给话术，info 走知识库，其余放行"""

    def test_action_gives_the_human_channel_everywhere(self):
        verdict = classify("我要退款")
        for page in ("chat", "agent", "rag", "graph"):
            with self.subTest(page=page):
                plan = handoff.decide(verdict, page)
                self.assertIn(handoff.HOTLINE, plan["reply"])
                self.assertFalse(plan["kb"])

    def test_info_goes_to_knowledge_base_not_hardcoded(self):
        """咨询类：不许本模块硬答，必须走知识库（答案来自知识库原文）"""
        with mock.patch.object(handoff, "_judge", stub_judge("info")):
            verdict = classify("退款需要多少天？")
        plan = handoff.decide(verdict, "graph")
        self.assertIsNone(plan["reply"], "咨询类问题不该由本模块写死回答")
        self.assertTrue(plan["kb"])
        self.assertEqual(plan["preset_intents"], ["knowledge"], "落地页应直接去知识库")
        self.assertTrue(handoff.decide(verdict, "chat")["kb"])
        self.assertTrue(handoff.decide(verdict, "agent")["kb"])
        self.assertIsNone(handoff.decide(verdict, "rag")["preset_intents"])

    def test_progress_with_id_goes_to_order_tool(self):
        """用户报了单号：直接查订单工具给真实状态"""
        with mock.patch.object(handoff, "_judge", stub_judge("progress")):
            verdict = classify("退款进度查一下 ORD-006")
        self.assertEqual(handoff.decide(verdict, "graph")["preset_intents"], ["order"])
        self.assertIsNone(handoff.decide(verdict, "agent")["reply"])
        self.assertIn("[[go:agent]]", handoff.decide(verdict, "chat")["reply"])

    def test_progress_without_id_never_interrogates(self):
        """没报单号：不许反问他单号（『问一圈』就是线上被骂的场景），用知识库答『进度在哪看』"""
        with mock.patch.object(handoff, "_judge", stub_judge("progress")):
            verdict = classify("昨天申请的退款还没到")
        self.assertEqual(handoff.decide(verdict, "graph")["preset_intents"], ["knowledge"])
        self.assertTrue(handoff.decide(verdict, "agent")["kb"])
        self.assertIsNone(handoff.decide(verdict, "rag")["preset_intents"])

    def test_mixed_and_none_are_never_blocked(self):
        for kind in ("mixed", "none"):
            with self.subTest(kind=kind):
                with mock.patch.object(handoff, "_judge", stub_judge(kind)):
                    verdict = classify("退款顺带问下耳机多少钱")
                for page in ("chat", "agent", "rag", "graph"):
                    plan = handoff.decide(verdict, page)
                    self.assertIsNone(plan["reply"])
                    self.assertFalse(plan["kb"])
                    self.assertIsNone(plan["preset_intents"])

    def test_whitelisted_by_security_layer(self):
        """退款问句在安全层就走白名单快路径，不花小模型的 token"""
        for text in ["我要退款", "退款政策是什么", "帮我退款 ORD-003"]:
            with self.subTest(text=text):
                self.assertTrue(rules.is_business(rules.normalize(text)))
                self.assertTrue(rules.whitelisted(rules.normalize(text)))


class TestScripts(JudgeTestCase):
    """话术质量：不能再出现"问一圈然后说办不了"的体验"""

    def test_never_asks_for_information(self):
        for text in ["我要退款", "我要投诉", "帮我把收货地址改一下"]:
            verdict = classify(text)
            with self.subTest(text=text):
                for script in (handoff.action_reply(verdict), handoff.chat_reply(verdict)):
                    self.assertNotRegex(script, r"(请|麻烦|需要)(您)?(提供|告诉|发|报)(一下)?[^。\n]{0,8}(订单号|用户\s*ID)")
                    self.assertNotIn("？", script)
                    self.assertNotIn("?", script)

    def test_always_gives_the_hotline(self):
        for text in ["我要退款", "我要投诉", "帮我把收货地址改一下"]:
            verdict = classify(text)
            with self.subTest(text=text):
                for script in (handoff.action_reply(verdict), handoff.chat_reply(verdict)):
                    self.assertIn(handoff.HOTLINE, script)
                    self.assertIn("tel:%s" % handoff.HOTLINE, script)

    def test_information_answers_do_not_push_the_human_channel(self):
        """咨询类问题不许自动补"请拨打人工"（线上反馈：只是在问，却被推去人工）"""
        with mock.patch.object(handoff, "_judge", stub_judge("info")):
            verdict = classify("退款需要多少天？")
        plan = handoff.decide(verdict, "rag")
        self.assertIsNone(plan["reply"], "咨询类不该由本模块插话")
        self.assertFalse(hasattr(handoff, "policy_footer"), "不该再有『回答后自动补人工』的函数")

    def test_script_is_short_enough_to_read_on_a_phone(self):
        verdict = classify("我要退款")
        self.assertLess(len(handoff.action_reply(verdict)), 420, "话术太长，手机上一屏看不完")


class TestConsistencyWithKnowledgeBase(JudgeTestCase):
    def _knowledge(self) -> str:
        path = os.path.join(os.path.dirname(__file__), "..", "app", "data", "knowledge", "policies.md")
        with open(os.path.abspath(path), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_hotline_matches_the_knowledge_base(self):
        self.assertIn(handoff.HOTLINE, self._knowledge(),
                      "话术里的电话和知识库不一致：%s" % handoff.HOTLINE)

    def test_knowledge_base_still_holds_the_timing_answers(self):
        """时效答案必须留在知识库里（由检索给出），不能搬进代码写死"""
        content = self._knowledge()
        for fact in ("3 个工作日", "3-5 个银行工作日"):
            with self.subTest(fact=fact):
                self.assertIn(fact, content)


if __name__ == "__main__":
    unittest.main()
