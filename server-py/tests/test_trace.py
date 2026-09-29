# -*- coding: utf-8 -*-
"""全链路追踪的黄金边界：埋点不拖累业务，落库失败不能带崩请求

这个文件盯住什么
----------------
1. **零成本**：没有追踪上下文时 trace_step / trace_span 是空操作；TRACE_ENABLED=false 时
   连上下文都不建。埋点写错不该让业务多花一毫秒。
2. **截断**：长文本、超大 detail、超多步骤都要按上限裁掉并**写明被裁了**——
   不写明的截断比不截断更坑（看的人以为数据是完整的）。
3. **落库失败静默**：追踪是运维工具，不是业务依赖。库连不上时 finish() 不能抛异常，
   请求该多快还多快（线上就是靠这条保证"追踪挂了也不影响客服"）。
4. **阶段即步骤**：RequestUsage.stage(...) 是全链路唯一现成的埋点入口，
   一次请求的每个环节（身份/安全/接力/检索/工具/回答…）都要原样进时间线，
   模型调用与检索明细在收尾时补齐（回调跑在别的线程，ContextVar 传不过去）。
"""
import asyncio
import time
import unittest
from unittest import mock

from app.observability import trace as trace_mod
from app.observability import usage as usage_mod


class TraceCase(unittest.TestCase):
    def setUp(self):
        # 每个用例自带干净的追踪上下文（ContextVar 是全局的，不清理会串味）
        trace_mod._current.set(None)
        self.addCleanup(trace_mod._current.set, None)

    def _start(self, **kwargs):
        return trace_mod.start_trace("chat", **kwargs)


class TestTraceCore(TraceCase):
    def test_no_context_is_free_and_finish_never_raises(self):
        """没有上下文时全是空操作；库不可用时落库失败也必须静默"""
        self.assertIsNone(trace_mod.current_trace())
        trace_mod.trace_step("llm", "没有上下文也得能调用")          # 不抛异常
        with trace_mod.trace_span("retrieval", "没有上下文"):
            pass
        async def _use_span():
            async with trace_mod.trace_span("llm", "异步空片段"):
                return True
        self.assertTrue(asyncio.run(_use_span()))
        self.assertIsNone(asyncio.run(trace_mod.finish_trace()))

        # TRACE_ENABLED=false：连上下文都不建
        with mock.patch.object(trace_mod, "TRACE_ENABLED", False):
            self.assertIsNone(trace_mod.start_trace("chat"))
            self.assertIsNone(trace_mod.current_trace())

        # 有上下文但数据库不可用：run_id 照常返回，不抛异常
        trace = self._start(run_id="run-1", user_id="U-100", user_name="李雷")
        trace_step_ok = trace is not None
        trace_mod.trace_step("guard", "安全防护", detail={"layer": "rule"})
        with mock.patch("app.db.postgres.get_pool", return_value=None):
            run_id = asyncio.run(trace.finish(status="ok"))
        self.assertTrue(trace_step_ok)
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(trace.steps), 1)
        self.assertEqual(trace.steps[0]["kind"], "guard")
        self.assertEqual(trace.status, "ok")

        # 库慢 / 连接池被打满：宁可丢这一次追踪，也不能让用户干等
        class _HangingPool:
            def acquire(self):
                return _HangingCtx()

        class _HangingCtx:
            async def __aenter__(self):
                await asyncio.sleep(30)

            async def __aexit__(self, *args):
                return False

        slow = self._start(run_id="run-slow")
        slow.step("llm", "模型调用 #1")
        started = time.time()
        with mock.patch("app.db.postgres.get_pool", return_value=_HangingPool()), \
                mock.patch.object(trace_mod, "WRITE_TIMEOUT_SECONDS", 0.05):
            run_id = asyncio.run(slow.finish(status="ok"))
        self.assertEqual(run_id, "run-slow")
        self.assertLess(time.time() - started, 1.0, "落库超时必须立刻放弃，而不是把请求挂住")

    def test_truncation_and_step_limit_are_visible(self):
        """截断必须写明，步骤超上限要记进 summary —— 报出来的数据不能假装完整"""
        trace = self._start()
        long_text = "极" * (trace_mod.MAX_STR + 50)
        trace.step("answer", "超长回答", detail={"text": long_text})
        detail = trace.steps[0]["detail"]
        self.assertIn("已截断", detail["text"])
        self.assertIn(str(len(long_text)), detail["text"])

        # 超大 detail：只留预览 + 真实字节数
        trace.step("retrieval", "超大明细", detail={"hits": [{"content": "x" * 500} for _ in range(200)]})
        packed = trace.steps[1]["detail"]
        self.assertTrue(packed.get("truncated"))
        self.assertGreater(packed.get("bytes", 0), trace_mod.MAX_DETAIL_BYTES)

        # 步骤上限：超出的丢弃，但要在 summary 里说实话
        before = len(trace.steps)
        for index in range(trace_mod.MAX_STEPS + 10):
            trace.step("llm", f"刷步骤 {index}")
        self.assertEqual(len(trace.steps), trace_mod.MAX_STEPS)
        self.assertGreater(before, 0)
        with mock.patch("app.db.postgres.get_pool", return_value=None):
            asyncio.run(trace.finish(status="ok"))
        # 上限之前一条不少，超出上限的如实报数（before + 310 - MAX_STEPS）
        self.assertEqual(trace.summary["droppedSteps"], before + 10,
                         "超上限被丢掉的步骤数要如实报出来")
        self.assertGreater(before, 0)


class TestUsageBridge(TraceCase):
    def test_usage_stages_and_details_become_timeline_steps(self):
        """stage(...) 原样进时间线；模型调用 / 检索明细在收尾时补齐；run_id 复用 trace_id"""
        import asyncio

        with mock.patch.object(usage_mod.redis_client, "get_redis", side_effect=RuntimeError("no redis")):
            usage = usage_mod.RequestUsage(route="agent", session_id="s-1",
                                           user_id="U-100", user_name="李雷")
            self.assertIs(usage.trace, trace_mod.current_trace())
            self.assertEqual(usage.trace.run_id, usage.trace_id)

            usage.set_question("我的订单到哪了")
            usage.stage("identity", user_id="U-100", claimed="")
            usage.stage("guard", layer="whitelist", category="business")
            usage.stage("handoff", kind="none", layer="rule")
            usage.stage("tool", tool="getOrderInfo", toolInput={"orderId": "ORD-004"},
                        observation='{"status":"已发货"}')
            usage.stage("grounding", blocked=False)
            usage.stage("answer", text="您的包裹已发出")
            usage.set_answer("您的包裹已发出")
            # 模型调用明细（回调采集的）与检索明细
            # on_llm_end 是这么累计的：明细进 calls，同时把总量加起来
            usage.callback.calls.append({"model": "deepseek-chat", "prompt_tokens": 120,
                                         "completion_tokens": 30, "total_tokens": 150,
                                         "latency_ms": 860, "prompt_preview": "你是极速购客服"})
            usage.callback.prompt_tokens, usage.callback.completion_tokens = 120, 30
            usage.callback.total_tokens, usage.callback.llm_calls = 150, 1
            usage.callback.models.append("deepseek-chat")
            usage.callback.retrievals.append({
                "query": "退款要几天到账", "embedded_query": "退款", "top_k": 4, "threshold": 0.4,
                "kept": 1, "filtered": False, "degraded": False, "error": "",
                "rerank": "阈值过滤 + 关键词召回 + RRF 融合",
                "rerank_stats": {"model": "BAAI/bge-reranker-v2-m3", "changed": 2},
                "hits": [{"source": "policies.md#退款流程", "score": 0.8263, "kept": True,
                          "match": "vector", "content": "退款原路返回"}],
            })
            payload = asyncio.run(usage.finish(status="ok"))

        self.assertEqual(usage.trace.question, "我的订单到哪了")
        self.assertEqual(payload["trace_id"], usage.trace_id)

        kinds = [step["kind"] for step in usage.trace.steps]
        # 阶段→类型映射：前端按 kind 给图标，映射错了页面就"看不出这条链路在干什么"
        for expected in ("identity", "guard", "handoff", "tool", "grounding", "answer", "llm",
                         "retrieval", "rerank", "response"):
            self.assertIn(expected, kinds)
        self.assertEqual(kinds.index("response"), len(kinds) - 1, "收尾步骤必须是最后一步")

        by_kind = {step["kind"]: step for step in usage.trace.steps}
        # 入参出参都要真的带上（排查就是靠这个点开看）
        self.assertEqual(by_kind["tool"]["detail"]["toolInput"], {"orderId": "ORD-004"})
        self.assertIn("已发货", by_kind["tool"]["detail"]["observation"])
        self.assertEqual(by_kind["llm"]["detail"]["total_tokens"], 150)
        self.assertEqual(by_kind["llm"]["duration_ms"], 860)
        self.assertEqual(by_kind["retrieval"]["detail"]["hits"][0]["source"], "policies.md#退款流程")
        self.assertEqual(by_kind["rerank"]["detail"]["changed"], 2)
        self.assertEqual(by_kind["response"]["detail"]["status"], "ok")
        self.assertEqual(usage.trace.status, "ok")
        self.assertEqual(usage.trace.summary["totalTokens"], 150)
        self.assertEqual(usage.trace.summary["retrievalCount"], 1)

    def test_blocked_request_is_marked_error_step(self):
        """被安全拦下 / 出错时，时间线上要一眼看出是"哪一步不合规"，状态不能都写成 ok"""
        import asyncio

        with mock.patch.object(usage_mod.redis_client, "get_redis", side_effect=RuntimeError("no redis")):
            usage = usage_mod.RequestUsage(route="chat", user_id="U-101")
            usage.stage("guard", layer="rule", category="leak_system_prompt")
            usage.stage("blocked", message="这条消息小购不能处理哦")
            usage.set_error("blocked/rule/leak_system_prompt")
            asyncio.run(usage.finish(status="blocked"))

        by_kind = {step["kind"]: step for step in usage.trace.steps}
        self.assertEqual(by_kind["guard"]["status"], "ok")
        self.assertEqual(by_kind["blocked"]["status"], "error", "被拦下的那一步要标成异常")
        self.assertEqual(usage.trace.status, "blocked")
        self.assertIn("leak_system_prompt", usage.trace.error)


if __name__ == "__main__":
    unittest.main()
