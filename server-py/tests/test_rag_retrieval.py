"""检索层单测：口语化查询归一 + 关键词兜底（不需要向量库/网络）"""
import unittest

from app.chains.query_utils import keyword_search, normalize_query


class TestNormalizeQuery(unittest.TestCase):
    def test_strip_colloquial_tail(self):
        cases = {
            "耳机咋卖": "耳机",
            "键盘怎么卖": "键盘",
            "耳机多少钱啊": "耳机",
            "退款要几天呢？": "退款要几天",
            "蓝牙耳机 X1 Pro 续航多久": "蓝牙耳机 X1 Pro 续航多久",
            "你好": "你好",
        }
        for raw, expect in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_query(raw), expect)

    def test_do_not_empty_the_query(self):
        # 整句都是语气词时不要削没了
        self.assertTrue(normalize_query("啊"))


class TestKeywordFallback(unittest.TestCase):
    """向量没召回时，关键词兜底必须能捞回正确章节"""

    def test_colloquial_earphone(self):
        sources = [(doc.metadata or {}).get("source") for doc, _ in keyword_search("耳机咋卖")]
        self.assertTrue(any("蓝牙耳机 X1 Pro" in (s or "") for s in sources), sources)

    def test_colloquial_keyboard(self):
        sources = [(doc.metadata or {}).get("source") for doc, _ in keyword_search("键盘咋卖")]
        self.assertIn("products.md#机械键盘 K200", sources)

    def test_marks_keyword_match(self):
        for doc, score in keyword_search("耳机咋卖"):
            with self.subTest(source=doc.metadata.get("source")):
                self.assertEqual(doc.metadata.get("match"), "keyword")
                self.assertGreater(score, 0)

    def test_no_match_for_irrelevant(self):
        self.assertEqual(keyword_search("今天天气怎么样"), [])


if __name__ == "__main__":
    unittest.main()
