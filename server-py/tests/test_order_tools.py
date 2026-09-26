"""订单工具单测：必须区分「不存在」和「没有数据」

回归的是线上真实问题：把 "U-109 不存在" 含糊成 "U-109 暂无订单"，
模型只能猜"可能没下过单，或者 ID 有误"。
"""
import json
import unittest

from app.tools.order_tools import (
    deterministic_lookup,
    get_logistics_tool,
    get_order_info_tool,
    get_user_orders_tool,
    known_user_ids,
)


class TestUnknownIds(unittest.TestCase):
    def test_unknown_user_reports_not_exist(self):
        payload = json.loads(get_user_orders_tool.invoke({"userId": "U-109"}))
        self.assertIn("不存在", payload["error"])
        self.assertNotIn("暂无订单", payload["error"])
        self.assertIn("knownUsers", payload)
        self.assertIn("hint", payload)

    def test_known_user_returns_orders(self):
        payload = json.loads(get_user_orders_tool.invoke({"userId": "U-102"}))
        self.assertIsInstance(payload, list)
        self.assertTrue(payload)

    def test_unknown_order_reports_not_exist(self):
        payload = json.loads(get_order_info_tool.invoke({"orderId": "ORD-999"}))
        self.assertIn("不存在", payload["error"])
        self.assertIn("hint", payload)

    def test_unknown_tracking_reports_not_exist(self):
        payload = json.loads(get_logistics_tool.invoke({"trackingNo": "SF0000000000"}))
        self.assertIn("不存在", payload["error"])

    def test_id_is_normalized(self):
        """用户输入小写/带空格的 ID 也能查到"""
        payload = json.loads(get_user_orders_tool.invoke({"userId": " u-102 "}))
        self.assertIsInstance(payload, list)

    def test_known_users_derived_from_orders(self):
        self.assertIn("U-100", known_user_ids())
        self.assertTrue(all(uid.startswith("U-") for uid in known_user_ids()))


class TestDeterministicLookup(unittest.TestCase):
    """模型没调工具时的兜底查询（线上编造订单表的事故防线）"""

    def test_unknown_user_returns_fact(self):
        result = deterministic_lookup("U-108 有哪些订单")
        self.assertIsNotNone(result)
        self.assertEqual(result["steps"][0]["tool"], "getUserOrders")
        self.assertIn("不存在", result["answer"])

    def test_extracts_multiple_ids(self):
        result = deterministic_lookup("帮我查 ORD-001，另外 U-102 有哪些订单")
        tools = [s["tool"] for s in result["steps"]]
        self.assertIn("getOrderInfo", tools)
        self.assertIn("getUserOrders", tools)

    def test_no_id_returns_none(self):
        self.assertIsNone(deterministic_lookup("你好呀"))
        self.assertIsNone(deterministic_lookup("我有哪些订单"))

    def test_facts_are_real_tool_output(self):
        result = deterministic_lookup("ORD-005")
        self.assertIn("ORD-005", result["answer"])
        self.assertIn("已发货", result["answer"])


if __name__ == "__main__":
    unittest.main()
