"""用量统计口径单测：错误率、平均值等派生指标算得对不对"""
import unittest

from app.observability.usage import _derive, _to_ints


class TestDerive(unittest.TestCase):
    def test_rates_and_averages(self):
        data = _derive({"requests": 4, "total_tokens": 1000, "latency_ms": 800, "errors": 1,
                        "prompt_tokens": 600, "completion_tokens": 400})
        self.assertEqual(data["avg_tokens"], 250.0)
        self.assertEqual(data["avg_latency_ms"], 200)
        self.assertEqual(data["error_rate"], 25.0)

    def test_zero_requests_no_division_error(self):
        data = _derive({})
        self.assertEqual(data["avg_tokens"], 0)
        self.assertEqual(data["avg_latency_ms"], 0)
        self.assertEqual(data["error_rate"], 0)

    def test_string_counters_coerced(self):
        data = _derive(_to_ints({"requests": "2", "total_tokens": "100", "errors": "1"}))
        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["error_rate"], 50.0)


if __name__ == "__main__":
    unittest.main()
