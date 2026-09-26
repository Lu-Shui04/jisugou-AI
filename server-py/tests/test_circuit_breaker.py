"""熔断器单测（离线、假时钟、不启动任何服务）

覆盖 20+ 种情况：三态转换、阈值口径、失败分类、半开探测、退避封顶、
dry-run、手动运维、并发、异步、注册表、依赖级配置覆盖，以及
最关键的"熔断绝不改变业务语义"（下游异常照常抛出）。
"""
import asyncio
import os
import threading
import unittest
from unittest import mock

from app.resilience import circuit as c


class FakeTime:
    """可控时钟：熔断的冷却时间不用真的等"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BreakerTestCase(unittest.TestCase):
    """每个用例：干净的环境变量 + 干净的表 + 可控时钟"""

    ENV = {
        "CIRCUIT_ENABLED": "true",
        "CIRCUIT_DRY_RUN": "false",
        "CIRCUIT_WINDOW_SECONDS": "60",
        "CIRCUIT_MIN_REQUESTS": "10",
        "CIRCUIT_FAILURE_RATE": "0.5",
        "CIRCUIT_CONSECUTIVE_FAILURES": "5",
        "CIRCUIT_OPEN_SECONDS": "30",
        "CIRCUIT_BACKOFF_MAX_SECONDS": "300",
        "CIRCUIT_HALF_OPEN_PROBES": "3",
        "CIRCUIT_JITTER": "0",
    }

    def setUp(self):
        patcher = mock.patch.dict(os.environ, self.ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 把可能存在的依赖级覆盖清掉（CIRCUIT_*_XXX 由旧用例留下会串味）
        for key in list(os.environ):
            if key.startswith("CIRCUIT_") and key not in self.ENV:
                self.addCleanup(os.environ.__setitem__, key, os.environ[key])
                os.environ.pop(key)
        self.clock = FakeTime()
        time_patcher = mock.patch.object(c, "time", self.clock)
        time_patcher.start()
        self.addCleanup(time_patcher.stop)
        self.breaker = c.CircuitBreaker("unit:test")

    def boom(self, times: int = 1, error: Exception | None = None) -> None:
        for _ in range(times):
            self.breaker.record_failure(error or TimeoutError("boom"))

    def good(self, times: int = 1) -> None:
        for _ in range(times):
            self.breaker.record_success()


class TestStates(BreakerTestCase):
    def test_01_closed_allows(self):
        allowed, reason = self.breaker.allow()
        self.assertTrue(allowed)
        self.assertEqual(reason, "closed")
        self.assertEqual(self.breaker.state(), c.CLOSED)

    def test_02_disabled_always_allows(self):
        with mock.patch.dict(os.environ, {"CIRCUIT_ENABLED": "false"}):
            self.boom(20)
            allowed, reason = self.breaker.allow()
        self.assertTrue(allowed)
        self.assertEqual(reason, "disabled")
        self.assertEqual(self.breaker.snapshot()["state"], c.CLOSED)

    def test_03_consecutive_failures_open(self):
        self.boom(4)
        self.assertEqual(self.breaker.state(), c.CLOSED, "4 次还不到阈值")
        self.boom(1)
        self.assertEqual(self.breaker.state(), c.OPEN, "连续 5 次应当打开")
        allowed, reason = self.breaker.allow()
        self.assertFalse(allowed)
        self.assertEqual(reason, "open")

    def test_04_failure_rate_opens(self):
        # 10 个请求里 5 个失败 = 50% 达到阈值（交替失败，确保走的是"失败率"而不是"连续失败"规则）
        for _ in range(5):
            self.boom(1)
            self.good(1)
        self.assertEqual(self.breaker.state(), c.OPEN)
        self.assertIn("失败率", self.breaker.snapshot()["last_open_reason"])

    def test_05_rate_below_threshold_stays_closed(self):
        self.good(8)
        self.boom(2)
        self.assertEqual(self.breaker.state(), c.CLOSED)

    def test_06_min_requests_guard(self):
        """样本太少不许熔断：半夜 3 个请求全失败也不能跳闸"""
        self.boom(3)
        self.assertEqual(self.breaker.state(), c.CLOSED)

    def test_07_window_slides(self):
        """老的失败滑出窗口后不再计入"""
        self.boom(4)
        self.clock.advance(61)
        self.breaker.allow()
        self.boom(1)
        self.assertEqual(self.breaker.state(), c.CLOSED, "窗口外的失败不该继续计数")

    def test_08_success_resets_consecutive(self):
        self.boom(4)
        self.good(1)
        self.boom(4)
        self.assertEqual(self.breaker.state(), c.CLOSED, "中间成功过，连续失败应从 0 重新算")


class TestHalfOpen(BreakerTestCase):
    def test_09_cooldown_then_half_open_probe_only_one(self):
        self.boom(5)
        self.clock.advance(31)
        allowed, reason = self.breaker.allow()
        self.assertTrue(allowed)
        self.assertEqual(reason, "half_open_probe")
        blocked, why = self.breaker.allow()
        self.assertFalse(blocked, "半开期间只放一个探测请求")
        self.assertEqual(why, "half_open_busy")

    def test_10_probe_success_recovers(self):
        self.boom(5)
        self.clock.advance(31)
        for _ in range(3):
            self.assertTrue(self.breaker.allow()[0])
            self.breaker.record_success()
        self.assertEqual(self.breaker.state(), c.CLOSED, "连续 3 次探测成功应恢复")
        self.assertEqual(self.breaker.snapshot()["opened_count"], 1)

    def test_11_probe_failure_reopens_with_backoff(self):
        self.boom(5)
        first = self.breaker.snapshot()["backoff_seconds"]
        self.clock.advance(31)
        self.breaker.allow()
        self.breaker.record_failure(TimeoutError("still down"))
        self.assertEqual(self.breaker.state(), c.OPEN)
        second = self.breaker.snapshot()["backoff_seconds"]
        self.assertGreater(second, first, "再次失败冷却要翻倍")

    def test_12_backoff_is_capped(self):
        for _ in range(8):
            self.boom(5)
            self.clock.advance(1000)
            self.breaker.allow()
            self.breaker.record_failure(TimeoutError("still down"))
        self.assertLessEqual(self.breaker.snapshot()["backoff_seconds"], 300)

    def test_13_force_half_open(self):
        self.boom(5)
        self.breaker.force_half_open()
        allowed, reason = self.breaker.allow()
        self.assertTrue(allowed)
        self.assertEqual(reason, "half_open_probe")


class TestFailureClassification(BreakerTestCase):
    def test_14_business_error_does_not_open(self):
        class BusinessError(Exception):
            business_error = True

        self.boom(10, BusinessError("订单不存在"))
        self.assertEqual(self.breaker.state(), c.CLOSED, "业务错误不该熔断")

    def test_15_http_4xx_not_counted_5xx_counted(self):
        class HttpError(Exception):
            def __init__(self, status):
                self.status_code = status

        self.boom(10, HttpError(400))
        self.assertEqual(self.breaker.state(), c.CLOSED, "400 不算下游故障")
        self.boom(5, HttpError(503))
        self.assertEqual(self.breaker.state(), c.OPEN, "503 要算")

    def test_16_rate_limit_and_timeout_counted(self):
        class HttpError(Exception):
            def __init__(self, status):
                self.status_code = status

        self.boom(5, HttpError(429))
        self.assertEqual(self.breaker.state(), c.OPEN, "429 限流要算")

    def test_17_circuit_open_error_not_counted(self):
        self.assertFalse(c.is_dependency_failure(c.CircuitOpenError("x", 1)))

    def test_18_timeout_and_connection_counted(self):
        self.assertTrue(c.is_dependency_failure(TimeoutError("t")))
        self.assertTrue(c.is_dependency_failure(ConnectionError("c")))


class TestGuard(BreakerTestCase):
    """guard：熔断不能改变业务语义"""

    def test_19_guard_passes_through_success(self):
        with self.breaker.guard():
            value = 42
        self.assertEqual(value, 42)
        self.assertEqual(self.breaker.state(), c.CLOSED)

    def test_20_guard_reraises_downstream_error(self):
        """下游异常必须原样抛出（调用方自己的降级逻辑不受影响）"""
        with self.assertRaises(TimeoutError):
            with self.breaker.guard():
                raise TimeoutError("down")
        self.assertEqual(self.breaker.snapshot()["failures"], 1)

    def test_21_guard_skips_body_when_open(self):
        self.boom(5)
        called = []
        with self.assertRaises(c.CircuitOpenError):
            with self.breaker.guard():
                called.append(1)  # pragma: no cover
        self.assertEqual(called, [], "打开状态下不许再调用下游")

    def test_22_guard_business_error_keeps_closed(self):
        class BusinessError(Exception):
            business_error = True

        for _ in range(10):
            with self.assertRaises(BusinessError):
                with self.breaker.guard():
                    raise BusinessError("订单 ORD-999 不存在")
        self.assertEqual(self.breaker.state(), c.CLOSED)

    def test_23_async_guard_behaves_the_same(self):
        async def run():
            with mock.patch.dict(os.environ, {"CIRCUIT_CONSECUTIVE_FAILURES": "2"}):
                for _ in range(2):
                    with self.assertRaises(TimeoutError):
                        async with self.breaker.aguard():
                            raise TimeoutError("down")
                with self.assertRaises(c.CircuitOpenError):
                    async with self.breaker.aguard():
                        pass  # pragma: no cover

        asyncio.run(run())

    def test_24_concurrent_guard_is_safe(self):
        """多线程并发：不炸、计数不错、打开后不再打下游"""
        unexpected: list = []
        rejected = []
        executed = []

        def worker():
            try:
                with self.breaker.guard():
                    executed.append(1)
                    raise TimeoutError("down")
            except TimeoutError:
                pass
            except c.CircuitOpenError:
                rejected.append(1)          # 快速失败，属于预期行为
            except Exception as err:  # pragma: no cover
                unexpected.append(err)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(unexpected, [], "并发下不该出现意料之外的异常")
        self.assertTrue(rejected, "打开之后应当有请求被快速失败")
        self.assertEqual(self.breaker.state(), c.OPEN)
        # 打开之后的下游调用次数应当明显少于总线程数
        self.assertLess(len(executed), 30)


class TestOperations(BreakerTestCase):
    def test_25_reset_closes_breaker(self):
        self.boom(5)
        self.breaker.reset()
        self.assertEqual(self.breaker.state(), c.CLOSED)
        self.assertTrue(self.breaker.allow()[0])

    def test_26_snapshot_fields(self):
        self.good(3)
        self.boom(1)
        snap = self.breaker.snapshot()
        self.assertEqual(snap["requests"], 4)
        self.assertEqual(snap["failures"], 1)
        self.assertEqual(snap["failure_rate"], 0.25)
        self.assertEqual(snap["opened_count"], 0)
        for key in ("name", "state", "enabled", "dry_run", "last_error", "backoff_seconds"):
            self.assertIn(key, snap)

    def test_27_registry_reuses_instances(self):
        first = c.get_breaker("registry:test")
        second = c.get_breaker("registry:test")
        self.assertIs(first, second)
        self.assertTrue(any(item["name"] == "registry:test" for item in c.snapshot_all()))

    def test_28_per_dependency_env_override(self):
        """依赖级配置覆盖：embedding 想熔断久一点就单独配"""
        with mock.patch.dict(os.environ, {"CIRCUIT_EMBEDDING_OPEN_SECONDS": "77"}):
            breaker = c.CircuitBreaker("embedding:zhipu")
            self.assertEqual(breaker.open_seconds, 77.0)
            other = c.CircuitBreaker("judge:security")
            self.assertEqual(other.open_seconds, 30.0)


class TestDryRun(BreakerTestCase):
    def test_29_dry_run_never_blocks(self):
        with mock.patch.dict(os.environ, {"CIRCUIT_DRY_RUN": "true"}):
            self.boom(30)
            allowed, reason = self.breaker.allow()
        self.assertTrue(allowed, "影子模式只统计、不拦")
        self.assertTrue(reason.startswith("dry_run"))
        self.assertEqual(self.breaker.state(), c.OPEN, "影子模式也要记录状态")

    def test_30_dry_run_guard_still_calls_downstream(self):
        with mock.patch.dict(os.environ, {"CIRCUIT_DRY_RUN": "true"}):
            self.boom(30)
            with self.breaker.guard():
                reached = True
        self.assertTrue(reached)


if __name__ == "__main__":
    unittest.main()