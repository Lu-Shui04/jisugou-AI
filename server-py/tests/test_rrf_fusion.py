"""RRF 融合与关键词检索的单测（不依赖向量库/网络）"""
import unittest

from langchain_core.documents import Document

from app.chains.query_utils import keyword_search, rrf_fuse


def make_doc(source: str):
    return Document(page_content="内容 " + source, metadata={"source": source, "section": source})


class TestRRF(unittest.TestCase):
    def test_both_lists_agree_ranks_higher(self):
        """两路都排第一的结果，融合后应该仍是第一"""
        vector = [(make_doc("A"), 0.7), (make_doc("B"), 0.6)]
        keyword = [(make_doc("A"), 0.33), (make_doc("C"), 0.32)]
        fused = rrf_fuse(vector, keyword, limit=3)
        self.assertEqual(fused[0][0].metadata["source"], "A")

    def test_keyword_only_hit_survives(self):
        """只在关键词路出现的结果也会被保留（这正是补召回的意义）"""
        vector = [(make_doc("A"), 0.7)]
        keyword = [(make_doc("B"), 0.33)]
        fused = rrf_fuse(vector, keyword, limit=2)
        self.assertEqual([d.metadata["source"] for d, _ in fused], ["A", "B"])

    def test_original_score_is_kept_for_display(self):
        """对外仍返回原始分数，不能变成 RRF 那种 0.016 的小数"""
        fused = rrf_fuse([(make_doc("A"), 0.4508)], [], limit=1)
        self.assertAlmostEqual(fused[0][1], 0.4508, places=4)
        self.assertIn("rrf_score", fused[0][0].metadata)

    def test_limit_applies(self):
        vector = [(make_doc(s), 0.5) for s in ["A", "B", "C", "D"]]
        self.assertEqual(len(rrf_fuse(vector, [], limit=2)), 2)


class TestKeywordStopwords(unittest.TestCase):
    def test_stopwords_do_not_cause_false_hits(self):
        """'python 怎么读取文件' 曾经因为'怎么'之类匹配到商品片段"""
        self.assertEqual(keyword_search("python 怎么读取文件"), [])

    def test_real_question_still_matches(self):
        sources = [(d.metadata or {}).get("source") for d, _ in keyword_search("耳机咋卖")]
        self.assertTrue(any("耳机" in (s or "") for s in sources), sources)


if __name__ == "__main__":
    unittest.main()
