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


if __name__ == "__main__":
    unittest.main()
