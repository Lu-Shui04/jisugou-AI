"""答案接地校验单测：模型编造订单号必须被拦下；已核实过的历史回答不能被误杀"""
import unittest

from langchain_core.messages import AIMessage, HumanMessage

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


class TestHistoryFacts(unittest.TestCase):
    """线上误杀事故的回归：会话历史里**已经核实过**的回答也要算事实来源

    现象：用户查完 U-103 的订单（ORD-008 / ORD-009）后只补一句 "U-103"，
          模型这一轮没调工具、直接沿用上文作答，出口校验只比对本轮工具事实，
          于是把 ORD-008 判成编造，整段换成"没能核实到"。
    """

    HISTORY = [
        {"role": "user", "content": "U-103"},
        {"role": "assistant", "content": "亲，U-103 名下有 ORD-008、ORD-009 两笔订单～"},
        {"role": "user", "content": "好的"},
    ]

    def test_echoing_verified_history_is_not_blocked(self):
        answer = "亲，您刚才那两笔是 ORD-008 和 ORD-009 哦～"
        self.assertFalse(check_answer(answer, "无")[0])  # 只看本轮事实 → 误判成编造
        ok, _ = check_answer(answer, facts_text("无", history_facts(self.HISTORY)))
        self.assertTrue(ok)  # 并入历史回答 → 通过

    def test_still_catches_newly_fabricated_order(self):
        facts = facts_text("无", history_facts(self.HISTORY))
        ok, detail = check_answer("亲，还有一笔 ORD-777 也是您的～", facts)
        self.assertFalse(ok)
        self.assertEqual(detail["offending"], ["ORD-777"])

    def test_langchain_messages_supported(self):
        """graph 链路传进来的是 LangChain 消息对象，不是 dict"""
        history = [AIMessage(content="查到 ORD-005 已发货"), HumanMessage(content="谢谢")]
        facts = history_facts(history)
        self.assertIn("ORD-005", facts)
        self.assertNotIn("谢谢", facts)  # 用户说的不算"已核实事实"


if __name__ == "__main__":
    unittest.main()
