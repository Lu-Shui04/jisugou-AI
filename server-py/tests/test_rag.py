"""RAG 检索层黄金边界测试：查询归一 / 关键词兜底 / RRF 融合 / 重排降级

这个文件盯住三件「改错就上线出问题」的事（合并自 test_rag_retrieval / test_rrf_fusion / test_rerank）：

1. 查询归一与关键词兜底：口语化尾巴会稀释向量相似度，去掉之后才过得了阈值；
   向量没召回时关键词兜底是最后一道网（要能捞回正确章节），同时停用词不能造成乱匹配。
2. RRF 融合：多路召回按排名融合排序，但**对外仍然返回原始相似度分** ——
   阈值过滤那套口径不能被 RRF 的 0.016 小数顶掉。
3. 重排（硅基流动 bge-reranker-v2-m3）：重排是「锦上添花」的一步，
   请求形状要对（地址 / 模型 / top_n 封顶 / 连接与读超时分开），重排分只进 metadata，
   任何失败（异常 / 5xx / 空结果 / 熔断打开）都必须原序降级、且熔断打开后一次请求都不再发。

全离线：不连向量库、不发真实 HTTP（httpx.Client 用桩替掉）。
"""
import os
import time
import unittest
from unittest import mock

import httpx
from langchain_core.documents import Document

from app.retrieval import rerank
from app.retrieval.query_utils import (
    build_source_line,
    is_small_talk,
    keyword_search,
    normalize_query,
    rrf_fuse,
)
from app.resilience import circuit


def make_doc(source: str):
    return Document(page_content="内容 " + source, metadata={"source": source, "section": source})


def make_hits(*sources):
    return [(Document(page_content="正文 " + s, metadata={"source": s, "section": s}), 0.42) for s in sources]


def sources_of(hits):
    return [doc.metadata.get("source") for doc, _ in hits]


# ────────────────────────────────────────────────────────────
# 一、查询归一 + 关键词兜底
# ────────────────────────────────────────────────────────────
class TestQueryNormalizeAndKeywordFallback(unittest.TestCase):

    def test_normalize_query_strips_colloquial_tail(self):
        """口语尾巴是召回的隐形杀手：'耳机咋卖' 的向量最高分只有 0.3858（被"咋卖"稀释，
        低于阈值 0.4 会一条都留不下），归一成 '耳机' 后能到 0.4508 才正常召回。
        这组取值是阈值线上的边界，不能被"顺手放宽正则"改掉。"""
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
        # 整句都是语气词时不能削成空串（原样退回，否则检索直接空查询）
        self.assertTrue(normalize_query("啊"))

    def test_keyword_fallback_recalls_sections_without_false_hits(self):
        """关键词兜底是向量没召回时的最后一道网：口语原句必须能捞回正确章节，
        返回的每条都要标 match=keyword 且分数 > 0（前端靠它区分来源）。
        反面同样重要：'python 怎么读取文件' 曾经因为"怎么"命中了商品片段，凭空给一句
        问候后面挂上"依据"；完全无关的句子必须一条都不返回（停用词就是为这个加的）。"""
        earphone_sources = [(doc.metadata or {}).get("source") for doc, _ in keyword_search("耳机咋卖")]
        self.assertTrue(any("蓝牙耳机 X1 Pro" in (s or "") for s in earphone_sources), earphone_sources)

        keyboard_sources = [(doc.metadata or {}).get("source") for doc, _ in keyword_search("键盘咋卖")]
        self.assertIn("products.md#机械键盘 K200", keyboard_sources)

        for doc, score in keyword_search("耳机咋卖"):
            with self.subTest(source=doc.metadata.get("source")):
                self.assertEqual(doc.metadata.get("match"), "keyword")
                self.assertGreater(score, 0)

        # 停用词不得造成误命中；无关问题必须颗粒无收
        self.assertEqual(keyword_search("python 怎么读取文件"), [])
        self.assertEqual(keyword_search("今天天气怎么样"), [])

        # 什么该检索、什么不该检索：寒暄几句就别去翻知识库了（kb_bridge 靠它短路），
        # 真正的业务问题必须照常检索
        for greeting in ("你好", "在吗", "谢谢"):
            with self.subTest(greeting=greeting):
                self.assertTrue(is_small_talk(greeting))
        for question in ("耳机咋卖", "我的快递到哪了", "退款要几天"):
            with self.subTest(question=question):
                self.assertFalse(is_small_talk(question))

        # 答案下面的"依据"那一行：有来源才拼，没有来源必须是空串（不能凭空写一行"依据："）
        line = build_source_line([{"source": "products.md#蓝牙耳机 X1 Pro", "score": 0.8263}])
        self.assertIn("蓝牙耳机 X1 Pro", line)
        self.assertEqual(build_source_line([]), "")


# ────────────────────────────────────────────────────────────
# 二、RRF 融合
# ────────────────────────────────────────────────────────────
class TestRRFFusion(unittest.TestCase):

    def test_both_lists_agree_ranks_higher_and_limit_applies(self):
        """两路都排第一的结果融合后必须仍是第一 —— 这是"补召回"的核心：
        向量 0.7 和关键词 0.33 不在一个量纲上，直接加权要调参且不稳，
        RRF 只看排名（k=60），两路都命中就天然叠加。limit 也要真的截断。"""
        vector = [(make_doc("A"), 0.7), (make_doc("B"), 0.6)]
        keyword = [(make_doc("A"), 0.33), (make_doc("C"), 0.32)]
        fused = rrf_fuse(vector, keyword, limit=3)
        self.assertEqual(fused[0][0].metadata["source"], "A")
        # A 在两路都是第 1，融合分必须高于只在一路出现的 B
        self.assertGreater(
            fused[0][0].metadata["rrf_score"],
            fused[1][0].metadata["rrf_score"],
        )

        vector4 = [(make_doc(s), 0.5) for s in ["A", "B", "C", "D"]]
        self.assertEqual(len(rrf_fuse(vector4, [], limit=2)), 2)

    def test_keyword_only_hit_survives_and_original_score_kept(self):
        """只在关键词路出现的结果也要保留（这正是补召回的意义，漏掉就等于白做一路）。
        更关键的是对外分数：必须还是原始相似度 0.4508，不能变成 RRF 那种 0.016 的小数 ——
        前端/后台要照常展示"相似度 0.4508"，阈值过滤也只认这个量纲，
        融排名用的 rrf_score 只写进 metadata 供链路追踪。"""
        fused = rrf_fuse([(make_doc("A"), 0.7)], [(make_doc("B"), 0.33)], limit=2)
        self.assertEqual(sources_of(fused), ["A", "B"])
        scores = {doc.metadata["source"]: score for doc, score in fused}
        self.assertAlmostEqual(scores["A"], 0.7, places=6)
        self.assertAlmostEqual(scores["B"], 0.33, places=6)

        one = rrf_fuse([(make_doc("A"), 0.4508)], [], limit=1)
        self.assertAlmostEqual(one[0][1], 0.4508, places=4)
        self.assertIn("rrf_score", one[0][0].metadata)


# ────────────────────────────────────────────────────────────
# 三、重排（全离线，用桩替掉 httpx）
# ────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            # 真实 httpx 抛的是 HTTPStatusError（带 response.status_code），
            # 熔断器靠这个区分"请求本身不对(4xx)"和"下游挂了(5xx/网络)"
            request = httpx.Request("POST", "https://api.siliconflow.cn/v1/rerank")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("HTTP %d" % self.status_code, request=request, response=response)

    def json(self):
        return self._payload


class FakeClient:
    """替掉 httpx.Client：记录请求，返回预置响应"""

    calls = []
    response = None
    error = None
    timeouts = []

    def __init__(self, timeout=None):
        self.timeout = timeout
        FakeClient.timeouts.append(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        FakeClient.calls.append({"url": url, "json": json, "headers": headers})
        if FakeClient.error is not None:
            raise FakeClient.error
        return FakeClient.response


class RerankTestBase(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.response = None
        FakeClient.error = None
        FakeClient.timeouts = []
        env = mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "sk-unit-test",
                                           "CIRCUIT_ENABLED": "true",
                                           "CIRCUIT_DRY_RUN": "false",
                                           "CIRCUIT_CONSECUTIVE_FAILURES": "2"})
        env.start()
        self.addCleanup(env.stop)
        enabled = mock.patch.object(rerank, "RERANK_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        patched = mock.patch("httpx.Client", FakeClient)
        patched.start()
        self.addCleanup(patched.stop)
        # 熔断器状态存在进程内存里，每个用例都必须从干净状态出发（否则用例顺序会互相污染）
        self.breaker = circuit.get_breaker(rerank.RERANK_BREAKER)
        self.breaker.reset()
        self.addCleanup(self.breaker.reset)


class TestRerank(RerankTestBase):

    def test_request_shape_top_n_capped_and_no_call_when_not_worth_it(self):
        """第一件事是"请求发对了"：地址 .../v1/rerank、Bearer Key、bge-reranker-v2-m3 模型名、
        不允许接口回传原文（候选正文本地就有，回传是纯浪费带宽）；
        top_n 即使配成 10，也必须封顶到候选条数（超了接口会报错）；
        连接超时 1.5s / 读超时 3s 必须分开设 —— 连都连不上就别傻等满 3s。
        另一半是"不值得调就别调"：开关关掉、候选少于 MIN_CANDIDATES、没 Key，
        都必须一条请求都不发（省调用是四层容错之一，也是线上账单的口子）。"""
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        rerank.rerank("退款几天到账", make_hits("退款.md", "配送.md"), top_n=10)

        call = FakeClient.calls[0]
        self.assertEqual(call["url"], "https://api.siliconflow.cn/v1/rerank")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-unit-test")
        payload = call["json"]
        self.assertEqual(payload["model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(payload["query"], "退款几天到账")
        self.assertEqual(payload["documents"], ["正文 退款.md", "正文 配送.md"])
        self.assertFalse(payload["return_documents"], "候选正文本地就有，不该让接口回传")
        self.assertEqual(payload["top_n"], 2, "top_n=10 但候选只有 2 条，必须封顶")

        timeout = FakeClient.timeouts[0]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertAlmostEqual(timeout.connect, rerank.RERANK_CONNECT_TIMEOUT, places=3)
        self.assertAlmostEqual(timeout.read, rerank.RERANK_TIMEOUT, places=3)

        # 候选只有 1 条（少于 RAG_RERANK_MIN_CANDIDATES）：不值得为一条结果调接口
        FakeClient.calls = []
        with mock.patch.object(rerank, "RERANK_ENABLED", True), \
                mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "sk-unit-test"}):
            ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md"))
        self.assertFalse(stats["enabled"])
        self.assertEqual(FakeClient.calls, [])
        self.assertEqual(len(ordered), 1)

        # 总开关关掉：直接短路，一条都不发，并且原样返回候选
        with mock.patch.object(rerank, "RERANK_ENABLED", False):
            ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertFalse(stats["enabled"])
        self.assertEqual(FakeClient.calls, [])
        self.assertEqual(sources_of(ordered), ["A.md", "B.md"])

        # Key 回退：只配了通用的 SILICONFLOW_API_KEY 时也要能重排；两个都空就别硬调
        with mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "", "SILICONFLOW_API_KEY": "sk-fallback"}):
            self.assertEqual(rerank.api_key(), "sk-fallback")
            self.assertTrue(rerank.enabled())
        with mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "", "SILICONFLOW_API_KEY": ""}):
            self.assertFalse(rerank.enabled())

    def test_reorders_by_rerank_score_and_keeps_original_score(self):
        """第二件事是"结果用对了"：按重排分重排名次，并把 rank_before / rank_after /
        rerank_score 写进 metadata（后台"重排前后名次变化"页就靠它），changed 要数对。
        最不能错的是对外分数：仍然是向量相似度 0.42 —— 下游 RAG_SCORE_THRESHOLD 是按相似度调的，
        被 0.8263 这种重排分量级顶掉就会穿透阈值，把不相关章节留在答案里。
        顺带守住 token 计费：硅基流动 meta.tokens.input_tokens 与 OpenAI 风格 usage.total_tokens
        两种返回都要认，都没有就得是 0（不能是 None，否则后台统计直接崩）。"""
        hits = make_hits("A.md", "B.md", "C.md")
        FakeClient.response = FakeResponse({
            "results": [
                {"index": 2, "relevance_score": 0.8263},
                {"index": 0, "relevance_score": 0.0837},
                {"index": 1, "relevance_score": 0.0008},
            ],
            "meta": {"tokens": {"input_tokens": 98, "output_tokens": 0}},
        })
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual(sources_of(ordered), ["C.md", "A.md", "B.md"])
        self.assertEqual(stats["after"], ["C.md", "A.md", "B.md"])
        self.assertEqual(stats["before"], ["A.md", "B.md", "C.md"])
        # 三个名次全部换位：[A,B,C] -> [C,A,B]
        self.assertEqual(stats["changed"], 3)
        self.assertEqual(stats["model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(stats["provider"], rerank.PROVIDER)
        self.assertTrue(stats["enabled"])
        self.assertEqual(stats["circuit"], "closed")
        self.assertEqual(stats["tokens"], 98)
        # 成功必须记成"健康"：正常调用被算成失败的话，重排会莫名其妙一直熔断
        snapshot = self.breaker.snapshot()
        self.assertEqual(snapshot["state"], circuit.CLOSED)
        self.assertEqual(snapshot["failures"], 0)

        hits = make_hits("A.md", "B.md")
        FakeClient.response = FakeResponse({
            "results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.02}],
            "usage": {"total_tokens": 123},
        })
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual(sources_of(ordered), ["B.md", "A.md"])
        top_doc, top_score = ordered[0]
        self.assertEqual(top_doc.metadata["rank_before"], 2)
        self.assertEqual(top_doc.metadata["rank_after"], 1)
        self.assertAlmostEqual(top_doc.metadata["rerank_score"], 0.91, places=6)
        # 对外分数仍是向量相似度分：阈值过滤那套口径不能被重排分顶掉
        self.assertAlmostEqual(top_score, 0.42, places=6)
        self.assertEqual(stats["tokens"], 123)

        # 两种 token 字段都没有 → 0
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        _, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(stats["tokens"], 0)

    def test_degrades_to_original_order_without_http_on_error_or_open_breaker(self):
        """第三件事，也是线上最要命的一件：重排是"锦上添花"，绝不能拖累问答。
        异常 / 5xx / 返回空 / index 越界 —— 一律原序返回 + stats 带 error，问答照常答；
        失败分类还要对：400 是"我们请求写错了"（例如模型名写错），下游其实是活的，
        不该把熔断打开（否则改错一次模型名会一直熔断）；5xx / 网络才算下游挂了。
        连续失败跳闸之后，必须**一次请求都不发**、毫秒级返回原顺序 ——
        否则一个挂掉的重排服务会让我们每个问题都干等 3s 超时，把整站拖垮。"""
        # ① 连接异常（如超时）：原序降级，错误信息要带出去给后台看
        hits = make_hits("A.md", "B.md")
        FakeClient.error = RuntimeError("connect timeout")
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual(sources_of(ordered), ["A.md", "B.md"])
        self.assertTrue(stats["enabled"])
        self.assertIn("connect timeout", stats["error"])
        self.assertEqual(stats["after"], ["A.md", "B.md"])
        self.breaker.reset()

        # ② 5xx：下游挂了，同样原序降级
        FakeClient.error = None
        FakeClient.response = FakeResponse({}, status_code=500)
        ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(sources_of(ordered), ["A.md", "B.md"])
        self.assertIn("error", stats)
        self.breaker.reset()

        # ③ 返回空 / index 越界（接口偶尔会回一个对不上的下标）：也算"这次没重排"
        for payload in ({"results": []}, {"results": [{"index": 99, "relevance_score": 0.9}]}):
            FakeClient.response = FakeResponse(payload)
            ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
            self.assertEqual(sources_of(ordered), ["A.md", "B.md"])
            self.assertEqual(stats["error"], "重排返回为空")
        self.breaker.reset()

        # ④ 400 连打 3 次也不能跳闸（业务错误按成功计）
        FakeClient.response = FakeResponse({}, status_code=400)
        for _ in range(3):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.CLOSED, self.breaker.snapshot())

        # ⑤ 503 连续 2 次（CIRCUIT_CONSECUTIVE_FAILURES=2）→ 跳闸
        FakeClient.response = FakeResponse({}, status_code=503)
        for _ in range(2):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.OPEN, self.breaker.snapshot())

        # ⑥ 熔断打开后：不发请求、毫秒级返回原顺序，问答照常答
        FakeClient.calls = []
        FakeClient.error = None
        FakeClient.response = FakeResponse({"results": [{"index": 1, "relevance_score": 0.9}]})
        started = time.perf_counter()
        ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        cost = time.perf_counter() - started
        self.assertEqual(FakeClient.calls, [], "熔断打开时不该再打接口")
        self.assertEqual(stats["circuit"], "open")
        self.assertEqual(stats["breaker"], rerank.RERANK_BREAKER)
        self.assertEqual(sources_of(ordered), ["A.md", "B.md"], "降级要保留原顺序")
        self.assertLess(cost, 0.05, "熔断打开必须毫秒级返回（实测 %.4fs）" % cost)


if __name__ == "__main__":
    unittest.main()
