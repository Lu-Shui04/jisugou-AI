"""重排（硅基流动 /rerank，BAAI/bge-reranker-v2-m3）单测：全离线，用桩替掉 httpx

覆盖三件事：
    1. 请求发对了：地址 .../v1/rerank、Bearer Key、模型名、top_n 不超过候选数
    2. 结果用对了：名次重排 + metadata 写入 rerank_score/rank_before/rank_after + token 两种返回都认
    3. 出错不砸主流程：HTTP 失败 / 返回空 → fail-open，原顺序返回
"""
import os
import time
import unittest
from unittest import mock

import httpx
from langchain_core.documents import Document

from app.retrieval import rerank
from app.resilience import circuit


def make_hits(*sources):
    return [(Document(page_content="正文 " + s, metadata={"source": s, "section": s}), 0.42) for s in sources]


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
        env = mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "sk-unit-test"})
        env.start()
        self.addCleanup(env.stop)
        enabled = mock.patch.object(rerank, "RERANK_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        patched = mock.patch("httpx.Client", FakeClient)
        patched.start()
        self.addCleanup(patched.stop)


class TestRequestShape(RerankTestBase):
    def test_01_calls_siliconflow_rerank_endpoint(self):
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        rerank.rerank("退款几天到账", make_hits("退款.md", "配送.md"))
        call = FakeClient.calls[0]
        self.assertEqual(call["url"], "https://api.siliconflow.cn/v1/rerank")
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk-unit-test")

    def test_02_uses_bge_reranker_model_and_no_documents_payload(self):
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        rerank.rerank("退款几天到账", make_hits("退款.md", "配送.md"))
        payload = FakeClient.calls[0]["json"]
        self.assertEqual(payload["model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(payload["query"], "退款几天到账")
        self.assertEqual(payload["documents"], ["正文 退款.md", "正文 配送.md"])
        self.assertFalse(payload["return_documents"], "候选正文本地就有，不该让接口回传")

    def test_03_top_n_never_exceeds_candidates(self):
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        rerank.rerank("退款几天到账", make_hits("退款.md", "配送.md"), top_n=10)
        self.assertEqual(FakeClient.calls[0]["json"]["top_n"], 2)

    def test_04_defaults_are_siliconflow(self):
        self.assertEqual(rerank.DEFAULT_BASE_URL, "https://api.siliconflow.cn/v1")
        self.assertEqual(rerank.DEFAULT_MODEL, "BAAI/bge-reranker-v2-m3")


class TestResultOrdering(RerankTestBase):
    def test_10_reorders_hits_by_rerank_score(self):
        hits = make_hits("A.md", "B.md", "C.md")
        FakeClient.response = FakeResponse({
            "results": [
                {"index": 2, "relevance_score": 0.8263},
                {"index": 0, "relevance_score": 0.0837},
                {"index": 1, "relevance_score": 0.0008},
            ],
        })
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["C.md", "A.md", "B.md"])
        self.assertEqual(stats["after"], ["C.md", "A.md", "B.md"])
        self.assertEqual(stats["before"], ["A.md", "B.md", "C.md"])
        # 三个名次全部换位：[A,B,C] -> [C,A,B]
        self.assertEqual(stats["changed"], 3)
        self.assertEqual(stats["model"], "BAAI/bge-reranker-v2-m3")
        self.assertEqual(stats["provider"], rerank.PROVIDER)
        self.assertTrue(stats["enabled"])

    def test_11_writes_rank_metadata_and_keeps_original_score(self):
        hits = make_hits("A.md", "B.md")
        FakeClient.response = FakeResponse({
            "results": [{"index": 1, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.02}],
        })
        ordered, _ = rerank.rerank("退款几天到账", hits)
        top_doc, top_score = ordered[0]
        self.assertEqual(top_doc.metadata["rank_before"], 2)
        self.assertEqual(top_doc.metadata["rank_after"], 1)
        self.assertAlmostEqual(top_doc.metadata["rerank_score"], 0.91, places=6)
        # 对外分数仍是向量相似度分：阈值过滤那套口径不能被重排分顶掉
        self.assertAlmostEqual(top_score, 0.42, places=6)


class TestTokenAccounting(RerankTestBase):
    def test_20_reads_siliconflow_meta_tokens(self):
        FakeClient.response = FakeResponse({
            "results": [{"index": 0, "relevance_score": 0.9}],
            "meta": {"tokens": {"input_tokens": 98, "output_tokens": 0}},
        })
        _, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(stats["tokens"], 98)

    def test_21_reads_openai_style_usage_tokens(self):
        FakeClient.response = FakeResponse({
            "results": [{"index": 0, "relevance_score": 0.9}],
            "usage": {"total_tokens": 123},
        })
        _, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(stats["tokens"], 123)

    def test_22_missing_token_fields_default_to_zero(self):
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        _, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(stats["tokens"], 0)


class TestFailOpen(RerankTestBase):
    def test_30_http_error_keeps_original_order(self):
        hits = make_hits("A.md", "B.md")
        FakeClient.error = RuntimeError("connect timeout")
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"])
        self.assertTrue(stats["enabled"])
        self.assertIn("connect timeout", stats["error"])
        self.assertEqual(stats["after"], ["A.md", "B.md"])

    def test_31_http_500_keeps_original_order(self):
        hits = make_hits("A.md", "B.md")
        FakeClient.response = FakeResponse({}, status_code=500)
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"])
        self.assertIn("error", stats)

    def test_32_empty_or_out_of_range_results_keeps_original_order(self):
        hits = make_hits("A.md", "B.md")
        FakeClient.response = FakeResponse({"results": []})
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"])
        self.assertEqual(stats["error"], "重排返回为空")

        FakeClient.response = FakeResponse({"results": [{"index": 99, "relevance_score": 0.9}]})
        ordered, stats = rerank.rerank("退款几天到账", hits)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"])
        self.assertEqual(stats["error"], "重排返回为空")


class TestResilience(RerankTestBase):
    """超时 / 熔断 / 降级三件套：重排是"锦上添花"，绝不能拖累问答"""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"CIRCUIT_ENABLED": "true",
                                           "CIRCUIT_DRY_RUN": "false",
                                           "CIRCUIT_CONSECUTIVE_FAILURES": "2"})
        env.start()
        self.addCleanup(env.stop)
        self.breaker = circuit.get_breaker(rerank.RERANK_BREAKER)
        self.breaker.reset()
        self.addCleanup(self.breaker.reset)

    def test_50_timeouts_split_connect_and_read(self):
        """连接超时 1.5s、读超时 3s：连不上就别等满 3s"""
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        timeout = FakeClient.timeouts[0]
        self.assertIsInstance(timeout, httpx.Timeout)
        self.assertAlmostEqual(timeout.connect, rerank.RERANK_CONNECT_TIMEOUT, places=3)
        self.assertAlmostEqual(timeout.read, rerank.RERANK_TIMEOUT, places=3)

    def test_51_repeated_failures_open_the_breaker(self):
        FakeClient.error = httpx.ConnectTimeout("connect timeout")
        for _ in range(2):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.OPEN, self.breaker.snapshot())

    def test_52_open_breaker_skips_http_and_degrades_instantly(self):
        """熔断打开后：不发请求、毫秒级返回原顺序，问答照常答"""
        FakeClient.error = httpx.ConnectTimeout("connect timeout")
        for _ in range(2):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.OPEN)

        FakeClient.calls = []
        FakeClient.error = None
        FakeClient.response = FakeResponse({"results": [{"index": 1, "relevance_score": 0.9}]})
        started = time.perf_counter()
        ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        cost = time.perf_counter() - started
        self.assertEqual(FakeClient.calls, [], "熔断打开时不该再打接口")
        self.assertEqual(stats["circuit"], "open")
        self.assertEqual(stats["breaker"], rerank.RERANK_BREAKER)
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"], "降级要保留原顺序")
        self.assertLess(cost, 0.05, "熔断打开必须毫秒级返回（实测 %.4fs）" % cost)

    def test_53_success_counts_as_healthy(self):
        FakeClient.response = FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]})
        _, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        snapshot = self.breaker.snapshot()
        self.assertEqual(snapshot["state"], circuit.CLOSED)
        self.assertEqual(snapshot["failures"], 0)
        self.assertEqual(stats["circuit"], "closed")

    def test_54_client_error_does_not_trip_the_breaker(self):
        """400 是"我们请求写错了"，不是下游挂了 —— 不该把熔断打开（否则改错模型名会一直熔断）"""
        FakeClient.response = FakeResponse({}, status_code=400)
        for _ in range(3):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.CLOSED, self.breaker.snapshot())

    def test_55_server_error_trips_the_breaker(self):
        FakeClient.response = FakeResponse({}, status_code=503)
        for _ in range(2):
            rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertEqual(self.breaker.state(), circuit.OPEN, self.breaker.snapshot())


class TestSwitchAndKey(unittest.TestCase):
    def test_40_disabled_switch_short_circuits_without_http(self):
        FakeClient.calls = []
        with mock.patch.object(rerank, "RERANK_ENABLED", False), \
                mock.patch("httpx.Client", FakeClient):
            ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md", "B.md"))
        self.assertFalse(stats["enabled"])
        self.assertEqual(FakeClient.calls, [])
        self.assertEqual([d.metadata["source"] for d, _ in ordered], ["A.md", "B.md"])

    def test_41_too_few_candidates_skips_the_call(self):
        FakeClient.calls = []
        with mock.patch.object(rerank, "RERANK_ENABLED", True), \
                mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "sk-unit-test"}), \
                mock.patch("httpx.Client", FakeClient):
            ordered, stats = rerank.rerank("退款几天到账", make_hits("A.md"))
        self.assertFalse(stats["enabled"])
        self.assertEqual(FakeClient.calls, [])
        self.assertEqual(len(ordered), 1)

    def test_42_key_falls_back_to_siliconflow_api_key(self):
        with mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "", "SILICONFLOW_API_KEY": "sk-fallback"}):
            self.assertEqual(rerank.api_key(), "sk-fallback")
        with mock.patch.object(rerank, "RERANK_ENABLED", True), \
                mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "", "SILICONFLOW_API_KEY": "sk-fallback"}):
            self.assertTrue(rerank.enabled())

    def test_43_no_key_means_not_enabled(self):
        with mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "", "SILICONFLOW_API_KEY": ""}), \
                mock.patch.object(rerank, "RERANK_ENABLED", True):
            self.assertFalse(rerank.enabled())

    def test_44_describe_shows_provider_and_model(self):
        with mock.patch.object(rerank, "RERANK_ENABLED", True), \
                mock.patch.dict(os.environ, {"RAG_RERANK_API_KEY": "sk-unit-test"}):
            text = rerank.describe()
        self.assertIn("硅基流动", text)
        self.assertIn(rerank.RERANK_MODEL, text)
        with mock.patch.object(rerank, "RERANK_ENABLED", False):
            self.assertIn("相似度阈值过滤", rerank.describe())


if __name__ == "__main__":
    unittest.main()
