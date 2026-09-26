"""答案接地校验单测：模型编造订单号必须被拦下"""
import unittest

from app.utils.grounding import UNGROUNDED_ANSWER, check_answer, sanitize, ungrounded_order_ids

REAL_FACTS = (
    '[{"orderId": "ORD-005", "status": "已发货", "amount": 1599.0},'
    ' {"orderId": "ORD-007", "status": "已取消", "amount": 249.0}]'
)


class TestGrounding(unittest.TestCase):
    def test_fabricated_order_id_is_caught(self):
        """线上真实事故：U-108 不存在，模型却编了一张订单表"""
        fabricated = (
            "亲，帮您查到用户 U-108 的订单列表如下：\n"
            "| ORD-003 | 已发货 | 299.0 元 | 智能手环 B5 |\n"
            "| ORD-005 | 已取消 | 599.0 元 | 便携蓝牙音箱 S3 |"
        )
        ok, detail = check_answer(fabricated, REAL_FACTS)
        self.assertFalse(ok)
        self.assertIn("ORD-003", detail["offending"])   # 事实里没有
        self.assertNotIn("ORD-005", detail["offending"])  # 事实里有，虽然内容是编的

    def test_grounded_answer_passes(self):
        answer = "亲，U-102 名下有 ORD-005（已发货）和 ORD-007（已取消）～"
        ok, _ = check_answer(answer, REAL_FACTS)
        self.assertTrue(ok)

    def test_answer_without_order_ids_passes(self):
        ok, _ = check_answer("亲，没有查到该用户的订单哦～", REAL_FACTS)
        self.assertTrue(ok)

    def test_all_invented_when_facts_are_error(self):
        error_facts = '{"error": "用户 U-108 不存在"}'
        answer = "亲，帮您查到 ORD-003、ORD-006 两笔订单～"
        ok, detail = check_answer(answer, error_facts)
        self.assertFalse(ok)
        self.assertEqual(len(detail["offending"]), 2)

    def test_sanitize_replaces_answer(self):
        answer, incident = sanitize("查到 ORD-999 了", REAL_FACTS, route="graph")
        self.assertEqual(answer, UNGROUNDED_ANSWER)
        self.assertIsNotNone(incident)
        self.assertEqual(incident["route"], "graph")

    def test_ungrounded_helper_case_insensitive(self):
        self.assertEqual(ungrounded_order_ids("ord-999", "ORD-001"), ["ORD-999"])


if __name__ == "__main__":
    unittest.main()
