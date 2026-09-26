"""订单工具单测：必须区分「不存在」和「没有数据」，并且只能查本人数据

回归两个真实问题：
1. 线上把 "U-109 不存在" 含糊成 "U-109 暂无订单"，模型只能猜"可能没下过单，或者 ID 有误"；
2. 更严重的：改造前工具完全不做归属校验，请求里把 userId 改成别人就能看别人的订单 ——
   现在每个工具都会先问一句"这条数据是你的吗"，本文件覆盖"越权一律拒绝"。
"""
import json
import unittest

from app.security import identity
from app.tools.order_tools import (
    deterministic_lookup,
    get_logistics_tool,
    get_order_info_tool,
    get_user_orders_tool,
    known_user_ids,
)


class TestUnknownIds(unittest.TestCase):
    """「不存在」和「没有数据」要分开说（在**有权**的前提下）"""

    def test_own_user_returns_orders(self):
        with identity.acting_as("U-102"):
            payload = json.loads(get_user_orders_tool.invoke({"userId": "U-102"}))
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)

    def test_user_without_orders_says_no_orders(self):
        """U-104：账号存在、名下一单都没有 —— 必须说"暂无订单"，不是"用户不存在" """
        with identity.acting_as("U-104"):
            payload = json.loads(get_user_orders_tool.invoke({"userId": "U-104"}))
        self.assertIn("名下暂无订单", payload["error"])
        self.assertNotIn("不存在", payload["error"])

    def test_other_user_is_forbidden_not_not_found(self):
        """查别人的用户 ID：拒绝（不是"不存在"—— 不能顺带告诉对方这个 ID 存不存在）"""
        with identity.acting_as("U-102"):
            payload = json.loads(get_user_orders_tool.invoke({"userId": "U-103"}))
        self.assertEqual(payload["code"], "FORBIDDEN")
        self.assertIn("无权查看", payload["error"])
        self.assertNotIn("ORD-008", json.dumps(payload, ensure_ascii=False))

    def test_unknown_order_reports_not_exist(self):
        with identity.acting_as("U-100"):
            payload = json.loads(get_order_info_tool.invoke({"orderId": "ORD-999"}))
        self.assertIn("不存在", payload["error"])
        self.assertIn("hint", payload)

    def test_unknown_tracking_reports_not_exist(self):
        with identity.acting_as("U-100"):
            payload = json.loads(get_logistics_tool.invoke({"trackingNo": "SF0000000000"}))
        self.assertIn("不存在", payload["error"])

    def test_id_is_normalized(self):
        """用户输入小写/带空格的 ID 也能查到"""
        with identity.acting_as("U-102"):
            payload = json.loads(get_user_orders_tool.invoke({"userId": " u-102 "}))
        self.assertIsInstance(payload, list)

    def test_known_users_include_account_without_orders(self):
        """用户表（账号表）而不是订单表：U-104 有账号没订单，同样"存在" """
        self.assertIn("U-100", known_user_ids())
        self.assertIn("U-104", known_user_ids())
        self.assertTrue(all(uid.startswith("U-") for uid in known_user_ids()))


class TestDeterministicLookup(unittest.TestCase):
    """模型没调工具时的兜底查询（线上编造订单表的事故防线）"""

    def test_foreign_user_returns_denial(self):
        """别人报了一个不属于自己的用户 ID：兜底查询也只回"无权查看"，不漏数据"""
        result = deterministic_lookup("U-108 有哪些订单", identity.Principal(
            user_id="U-102", user_name="王强", authenticated=True, reason="ok"))
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getUserOrders")
        self.assertIn("无权查看", result["answer"])

    def test_extracts_multiple_ids(self):
        result = deterministic_lookup("帮我查 ORD-001，另外 U-102 有哪些订单")
        tools = [s["tool"] for s in result["steps"]]
        self.assertIn("getOrderInfo", tools)
        self.assertIn("getUserOrders", tools)

    def test_no_id_returns_none(self):
        self.assertIsNone(deterministic_lookup("你好呀"))
        self.assertIsNone(deterministic_lookup("我有哪些订单"))

    def test_facts_are_real_tool_output(self):
        with identity.acting_as("U-102"):
            result = deterministic_lookup("ORD-005")
        self.assertIn("ORD-005", result["answer"])
        self.assertIn("已发货", result["answer"])

    def test_dashless_lowercase_user_id(self):
        """线上用户会发 "u103给我看一下物流"（小写、漏连字符、中文紧跟着）"""
        with identity.acting_as("U-103"):
            result = deterministic_lookup("u103给我看一下物流")
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getUserOrders")
        self.assertEqual(result["steps"][0]["input"]["userId"], "U-103")
        self.assertIn("ORD-008", result["answer"])

    def test_dashless_order_id(self):
        """ORD-006 是 U-101 的订单，所以这里也顺便验证"查自己的单"能过"""
        with identity.acting_as("U-101"):
            result = deterministic_lookup("ord006 到哪了")
        self.assertEqual(result["steps"][0]["tool"], "getOrderInfo")
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-006")
        self.assertNotIn("不存在", result["answer"])

    def test_no_principal_is_anonymous(self):
        """没有身份时兜底查询同样 fail closed"""
        result = deterministic_lookup("ORD-001")
        self.assertIn("未登录", result["answer"])



class TestShorthandOrderIds(unittest.TestCase):
    """省略写法 / 指代也要能落到具体订单上

    线上实测：用户看完订单一览后追问「004为什么没有下单时间」。
    "004" 抠不出 ORD 号 → 模型凭上文记忆作答 → 出口接地校验判 ORD-004 不在事实里 →
    整段被换成"这笔数据没能核实到"（而工具其实查得到，四笔订单每笔都带 createTime）。
    """

    HISTORY = [
        {"role": "user", "content": "查一下我的订单"},
        {"role": "assistant", "content": "您有两笔已发货订单：ORD-001 蓝牙耳机、ORD-010 机械键盘。"},
    ]

    def test_bare_digits_resolved_from_history(self):
        with identity.acting_as("U-100"):
            result = deterministic_lookup("004为什么没有下单时间", history=self.HISTORY)
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getOrderInfo")
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-004")
        self.assertIn("2025-03-15", result["answer"])   # createTime 真的带回来了

    def test_nth_order_reference(self):
        with identity.acting_as("U-100"):
            result = deterministic_lookup("第二笔到哪了", history=self.HISTORY)
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-010")

    def test_anaphora_with_order_topic(self):
        with identity.acting_as("U-100"):
            result = deterministic_lookup("这单发货了吗", history=self.HISTORY)
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-010")

    def test_own_orders_used_when_history_is_empty(self):
        """新会话直接问 "004"：用本人名下的订单号补全（只认自己的，不猜别人的）"""
        with identity.acting_as("U-100"):
            result = deterministic_lookup("004 到哪了")
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-004")

    def test_foreign_shorthand_does_not_resolve(self):
        """U-102 名下没有 ORD-001 附近的号：补不出来就返回 None，不拿别人的号去试"""
        with identity.acting_as("U-102"):
            self.assertIsNone(deterministic_lookup("001为什么没有下单时间"))

    def test_amounts_are_not_treated_as_order_numbers(self):
        """金额/数量这类数字不能被当成订单号后缀（138.9 元不是 ORD-138）"""
        with identity.acting_as("U-100"):
            self.assertIsNone(deterministic_lookup("为什么扣了我 138 元", history=self.HISTORY))

    def test_ambiguous_shorthand_is_not_guessed(self):
        """一句话里提到两个省略号："001 和 011 哪个先到" —— 猜哪个都不对，返回 None"""
        history = [{"role": "assistant", "content": "ORD-001 已发货；ORD-011 待发货"}]
        with identity.acting_as("U-100"):
            self.assertIsNone(deterministic_lookup("001 和 011 哪个先到", history=history))

    def test_full_id_still_wins(self):
        with identity.acting_as("U-100"):
            result = deterministic_lookup("ORD-004 详情", history=self.HISTORY)
        self.assertEqual(result["steps"][0]["input"]["orderId"], "ORD-004")


if __name__ == "__main__":
    unittest.main()