"""提示词回归：钉住三条"线上踩过坑"的规则，别在后续编辑里被悄悄删掉

为什么要测提示词：这套系统的行为边界大半写在提示词里，
删掉一句不会报错、单测也全绿，但用户那边立刻退化。三条规则各自的来历：

  A 物流追问  ：「我的订单到哪里了？」原来只回一份订单列表 + 反问"您想查哪一个"，
                线上实测 6/6 都没真去查轨迹（智能中枢同一句话却会查）。
  B 字段甩锅  ：模型把表格里漏填的 createTime 说成"系统返回的摘要里没有这个字段"，
                而工具实测四笔订单都带 createTime —— 把自己的疏漏甩锅给数据源。
  C 凭记忆作答：用户问"004为什么没有下单时间"时，模型不调工具、凭上文记忆作答，
                出口接地校验只认本轮工具事实，于是整段被拦成"这笔数据没能核实到"。

改完结构以后，提示词全部集中在 app/prompts/，所以这里直接读那一层。
"""
import os
import unittest

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "prompts")
ORDER_PROMPTS = os.path.join(BASE, "order_agent.py")
SYNTHESIZER_PROMPT = os.path.join(BASE, "graph.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class TestOrderPromptRules(unittest.TestCase):
    """订单查询页 / 智能中枢订单节点，两份提示词都要守住同样的三条"""

    def setUp(self):
        self.text = _read(ORDER_PROMPTS)

    def test_rule_a_logistics_follow_through(self):
        """问"我的订单到哪里了"要顺着已发货订单查轨迹，不是反问用户"""
        self.assertIn("已发货", self.text)
        self.assertIn("getLogisticsInfo", self.text)

    def test_rule_b_never_blame_the_data_source(self):
        """自己漏填的字段不许说成"系统没有返回" """
        self.assertIn("系统没有返回", self.text)
        self.assertIn("漏填", self.text)

    def test_rule_c_lookup_before_answering_about_one_order(self):
        """提到某一笔具体订单要先查再答（否则会被出口接地校验拦成"没能核实到"）"""
        self.assertIn("getOrderInfo", self.text)

    def test_synthesizer_also_guards_against_blaming_data(self):
        """最终答复由合成节点写，这条约束不能只写在订单节点里"""
        self.assertIn("系统没有返回", _read(SYNTHESIZER_PROMPT))


if __name__ == "__main__":
    unittest.main()
