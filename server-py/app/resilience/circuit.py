"""熔断器（Circuit Breaker）

要解决的问题
------------
下游整体故障时，「超时 + 重试」反而会放大故障：
我们每个请求本来就要等小模型 3s 超时、等 embedding 15s x 3 次重试、等主模型 30s，
并发一上来这些"傻等"会把进程的连接与线程占满，一个下游挂掉拖垮整站。

所以补上第三件事：**熔断**。
    重试是"再试一次"；熔断是"别再试了"。

三态机（业界标准语义）
----------------------
    CLOSED    正常放行，统计成败
              → 窗口内请求数够 + 失败率超标（或连续失败太多） → OPEN
    OPEN      快速失败，一次都不打下游；冷却时间到 → HALF_OPEN
    HALF_OPEN 只放行少量探测请求；连续探测成功 → CLOSED；探测失败 → 回 OPEN，冷却翻倍

几条不写清楚就会踩的原则
------------------------
1. **最小样本量**：请求太少时只统计不熔断，避免半夜 3 个请求失败就跳闸。
2. **失败分类**：只有"下游故障"（超时 / 连接错误 / 5xx / 429）计入失败；
   业务错误（订单不存在、参数错误等 4xx）说明下游是活的，不计入。
3. **绝不打断业务**：本模块任何异常都不许冒泡；熔断器自己出错就当没熔断。
4. **降级路径必须由调用方给定**：CircuitOpenError 由调用方捕获后走既有兜底
   （小模型 → fail-open；embedding → 关键词召回；主模型 → 备用模型），不直接抛给用户。

配置（.env，可对单个依赖覆盖，例如 CIRCUIT_EMBEDDING_OPEN_SECONDS=60）
----------------------------------------------------------------------
    CIRCUIT_ENABLED=true
    CIRCUIT_DRY_RUN=false            # 只统计不拦（影子模式，先观察再开）
    CIRCUIT_WINDOW_SECONDS=60
    CIRCUIT_MIN_REQUESTS=10
    CIRCUIT_FAILURE_RATE=0.5
    CIRCUIT_CONSECUTIVE_FAILURES=5
    CIRCUIT_OPEN_SECONDS=30
    CIRCUIT_BACKOFF_MAX_SECONDS=300
    CIRCUIT_HALF_OPEN_PROBES=3       # 连续探测成功几次算恢复
    CIRCUIT_JITTER=0.2               # 冷却抖动，避免多个实例同时恢复打崩下游

说明：状态保存在进程内存里。服务是 uvicorn --workers 2，两个 worker 各自计数、
各自跳闸（效果上依然是"快速失败 + 自动恢复"）。要严格跨进程共享，把状态换到
Redis 即可（见文件末尾的说明），当前先保证简单、可预测、不影响业务。
"""
import logging
import os
import random
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

logger = logging.getLogger("jisu.circuit")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


# ── 配置 ────────────────────────────────────────────────────────
def _env(name: str, key: str, default: Any) -> Any:
    """先看依赖级覆盖，再看全局（CIRCUIT_OPEN_SECONDS）

    依赖级支持三种写法，挑最顺手的用：
        embedding:zhipu  →  CIRCUIT_EMBEDDING_ZHIPU_OPEN_SECONDS
                         /  CIRCUIT_EMBEDDING_OPEN_SECONDS
                         /  CIRCUIT_ZHIPU_OPEN_SECONDS
    """
    parts = [part for part in (name or "").split(":") if part]
    slugs = []
    if parts:
        slugs.append("_".join(parts))
        if len(parts) > 1:
            slugs.append(parts[0])
            slugs.append(parts[-1])
    raw = None
    for slug in slugs:
        candidate = "CIRCUIT_%s_%s" % (slug.upper().replace("-", "_"), key)
        if candidate in os.environ:
            raw = os.environ[candidate]
            break
    if raw is None:
        raw = os.getenv("CIRCUIT_%s" % key, default)
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def circuit_enabled() -> bool:
    return _env("", "ENABLED", True)


def circuit_dry_run() -> bool:
    return _env("", "DRY_RUN", False)


# ── 异常 ────────────────────────────────────────────────────────
class CircuitOpenError(Exception):
    """熔断打开时的快速失败异常（调用方应捕获并走降级）"""

    def __init__(self, name: str, retry_after: float = 0.0, reason: str = ""):
        self.name = name
        self.retry_after = max(0.0, float(retry_after))
        self.reason = reason or "熔断已打开"
        super().__init__("%s 熔断中（%.0fs 后可重试）" % (name, self.retry_after))


BUSINESS_ERROR_FLAG = "business_error"


def is_dependency_failure(error: BaseException) -> bool:
    """是不是"下游故障"（只有这类才计入熔断）

    - 业务错误（订单不存在、参数错误…）说明下游是活的 → 不计入
    - 熔断自身抛出的异常 → 不计入（否则会把熔断算成失败，永远恢复不了）
    """
    if isinstance(error, CircuitOpenError):
        return False
    if getattr(error, BUSINESS_ERROR_FLAG, False):
        return False

    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        # 4xx 大多是"请求本身不对"，除了 408 超时与 429 限流
        if 400 <= status < 500 and status not in (408, 429):
            return False
        return True

    # 网络类异常一律算下游故障
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True

    return True


# ── 熔断器 ──────────────────────────────────────────────────────
class CircuitBreaker:
    def __init__(self, name: str):
        self.name = name
        self._lock = threading.RLock()
        self._window: deque = deque()          # [(ts, ok)]
        self._consecutive_failures = 0
        self._state = CLOSED
        self._opened_at = 0.0
        self._opened_count = 0
        self._backoff_level = 0
        self._probing = False
        self._probe_success = 0
        self._last_error = ""
        self._last_open_reason = ""
        self._cooling_until = 0.0

    # ── 配置读取（每次取，方便测试与热调整）─────────────────────
    @property
    def window_seconds(self) -> float:
        return _env(self.name, "WINDOW_SECONDS", 60.0)

    @property
    def min_requests(self) -> int:
        return _env(self.name, "MIN_REQUESTS", 10)

    @property
    def failure_rate(self) -> float:
        return _env(self.name, "FAILURE_RATE", 0.5)

    @property
    def consecutive_failures(self) -> int:
        return _env(self.name, "CONSECUTIVE_FAILURES", 5)

    @property
    def open_seconds(self) -> float:
        return _env(self.name, "OPEN_SECONDS", 30.0)

    @property
    def backoff_max(self) -> float:
        return _env(self.name, "BACKOFF_MAX_SECONDS", 300.0)

    @property
    def half_open_probes(self) -> int:
        return max(1, _env(self.name, "HALF_OPEN_PROBES", 3))

    @property
    def jitter(self) -> float:
        return _env(self.name, "JITTER", 0.2)

    @property
    def enabled(self) -> bool:
        return circuit_enabled()

    @property
    def dry_run(self) -> bool:
        return circuit_dry_run()

    # ── 状态查询 ────────────────────────────────────────────────
    def state(self) -> str:
        with self._lock:
            return self._state

    def retry_after(self) -> float:
        with self._lock:
            return max(0.0, self._cooling_until - time.monotonic())

    def _cooldown(self) -> float:
        """冷却时间：每失败一次翻倍，封顶，再加抖动"""
        base = min(self.open_seconds * (2 ** self._backoff_level), self.backoff_max)
        if self.jitter <= 0:
            return base
        spread = base * self.jitter
        return max(1.0, base + random.uniform(-spread, spread))

    def _prune(self, now: float) -> None:
        limit = now - self.window_seconds
        while self._window and self._window[0][0] < limit:
            self._window.popleft()

    def _trailing_failures(self) -> int:
        """窗口末尾连续失败了多少次

        从窗口推导而不是单独计数：这样"连续失败"会跟着窗口一起衰减 ——
        十分钟前的 4 次失败不该和现在这一次凑成"连续 5 次"。
        """
        count = 0
        for _, ok in reversed(self._window):
            if ok:
                break
            count += 1
        return count

    # ── 放行判断 ────────────────────────────────────────────────
    def allow(self) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。dry-run 模式下永远放行，只做统计。"""
        if not self.enabled:
            return True, "disabled"

        with self._lock:
            now = time.monotonic()
            self._prune(now)

            if self._state == OPEN:
                if now < self._cooling_until:
                    if self.dry_run:
                        return True, "dry_run_open"
                    return False, "open"
                # 冷却结束 → 半开，只放一个探测
                self._state = HALF_OPEN
                self._probing = False
                self._probe_success = 0
                logger.info("熔断器 %s 进入半开，放行探测请求", self.name)

            if self._state == HALF_OPEN:
                if self._probing:
                    if self.dry_run:
                        return True, "dry_run_half_open"
                    return False, "half_open_busy"
                self._probing = True
                return True, "half_open_probe"

            return True, "closed"

    # ── 结果记录 ────────────────────────────────────────────────
    def record_success(self, probe: Optional[bool] = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._window.append((now, True))
            self._consecutive_failures = self._trailing_failures()

            if self._state == HALF_OPEN:
                self._probing = False
                self._probe_success += 1
                if probe is False or self._probe_success >= self.half_open_probes:
                    self._close_locked()
                    logger.info("熔断器 %s 探测通过，恢复（closed）", self.name)
                return

            self._maybe_open_locked(now)

    def record_failure(self, error: Optional[BaseException] = None) -> None:
        # 业务错误（订单不存在、400 参数错…）说明下游是活的：按成功计，避免误熔断。
        # 放在这里而不是只放在 guard 里，是为了让所有调用方（包括以后新加的）默认就是对的。
        if error is not None and not is_dependency_failure(error):
            self.record_success()
            return
        if not self.enabled:
            return
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._window.append((now, False))
            self._consecutive_failures = self._trailing_failures()
            if error is not None:
                self._last_error = "%s: %s" % (type(error).__name__, str(error)[:160])

            if self._state == HALF_OPEN:
                self._probing = False
                self._probe_success = 0
                self._backoff_level += 1
                self._open_locked(now, "半开探测失败")
                return

            if self._state == OPEN:
                return

            self._maybe_open_locked(now)

    def _maybe_open_locked(self, now: float) -> None:
        """统一判断要不要打开（失败与成功事件都会过一遍）"""
        self._consecutive_failures = self._trailing_failures()
        total = len(self._window)
        failures = sum(1 for _, ok in self._window if not ok)
        rate = failures / total if total else 0.0

        if self._consecutive_failures >= self.consecutive_failures:
            self._open_locked(now, "连续失败 %d 次" % self._consecutive_failures)
        elif total >= self.min_requests and rate >= self.failure_rate:
            self._open_locked(now, "失败率 %.0f%%（%d/%d）" % (rate * 100, failures, total))

    def _open_locked(self, now: float, reason: str) -> None:
        self._state = OPEN
        self._opened_at = now
        self._opened_count += 1
        self._probing = False
        self._probe_success = 0
        self._cooling_until = now + self._cooldown()
        self._last_open_reason = reason
        logger.warning("熔断器 %s 打开：%s（%.0fs 后自动试探）",
                       self.name, reason, self.retry_after())

    def _close_locked(self) -> None:
        self._state = CLOSED
        self._window.clear()
        self._consecutive_failures = 0
        self._backoff_level = 0
        self._probing = False
        self._probe_success = 0
        self._cooling_until = 0.0
        self._last_open_reason = ""

    # ── 运维动作 ────────────────────────────────────────────────
    def reset(self) -> None:
        """手动复位（下游已确认恢复，不想等冷却）"""
        with self._lock:
            self._close_locked()
        logger.info("熔断器 %s 被手动复位", self.name)

    def force_half_open(self) -> None:
        """强制进入半开，立刻放一个探测请求"""
        with self._lock:
            self._state = HALF_OPEN
            self._probing = False
            self._probe_success = 0
            self._cooling_until = 0.0
        logger.info("熔断器 %s 被强制半开探测", self.name)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            total = len(self._window)
            failures = sum(1 for _, ok in self._window if not ok)
            return {
                "name": self.name,
                "state": self._state,
                "dry_run": self.dry_run,
                "enabled": self.enabled,
                "window_seconds": self.window_seconds,
                "requests": total,
                "failures": failures,
                "failure_rate": round(failures / total, 3) if total else 0.0,
                "consecutive_failures": self._consecutive_failures,
                "opened_count": self._opened_count,
                "backoff_seconds": round(self._cooling_until - now, 1) if self._state == OPEN else 0.0,
                "last_open_reason": self._last_open_reason,
                "last_error": self._last_error,
            }

    # ── 上下文管理器（同步 / 异步）───────────────────────────────
    @contextmanager
    def guard(self):
        allowed, reason = self.allow()
        if not allowed:
            raise CircuitOpenError(self.name, self.retry_after(), reason)
        probe = reason == "half_open_probe"
        try:
            yield
        except Exception as error:
            if is_dependency_failure(error):
                self.record_failure(error)
            else:
                # 业务错误：下游是活的，按成功计，避免误熔断
                self.record_success(probe=probe)
            raise
        else:
            self.record_success(probe=probe)

    @asynccontextmanager
    async def aguard(self):
        allowed, reason = self.allow()
        if not allowed:
            raise CircuitOpenError(self.name, self.retry_after(), reason)
        probe = reason == "half_open_probe"
        try:
            yield
        except Exception as error:
            if is_dependency_failure(error):
                self.record_failure(error)
            else:
                self.record_success(probe=probe)
            raise
        else:
            self.record_success(probe=probe)


# ── 注册表 ──────────────────────────────────────────────────────
_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.RLock()

# 依赖清单（名字即"熔断粒度"）：llm:chat / llm:chat:fallback / embedding:zhipu /
# judge:security / judge:handoff
# 注：rerank（硅基流动 bge-reranker-v2-m3）没接熔断器，它的容错是
# RAG_RERANK_TIMEOUT_SECONDS 超时 + fail-open（重排失败就保持原顺序）
def get_breaker(name: str) -> CircuitBreaker:
    with _registry_lock:
        breaker = _breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name)
            _breakers[name] = breaker
        return breaker


def snapshot_all() -> list[dict]:
    with _registry_lock:
        return [breaker.snapshot() for breaker in _breakers.values()]


def reset_all() -> int:
    with _registry_lock:
        for breaker in _breakers.values():
            breaker.reset()
        return len(_breakers)


# 说明：状态当前存在进程内存。若将来要跨 worker / 跨实例共享，只需在这里加一层
# Redis 读写（把 state / opened_at / backoff_level 存成 hash，读加 1s 本地缓存、
# 写做 0.5s 节流，Redis 不可用就退回内存）。业务侧代码不需要任何改动。
