"""熔断（Circuit Breaker）

外部依赖（对话模型 / Embedding / 判定小模型 / 向量库）整体挂掉时的自我保护：
失败到这程度就别再傻等了，先快速失败、走降级，过一会儿再试探恢复。
"""
from app.resilience.circuit import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
    get_breaker,
    is_dependency_failure,
    snapshot_all,
)

__all__ = [
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "CircuitBreaker",
    "CircuitOpenError",
    "get_breaker",
    "is_dependency_failure",
    "snapshot_all",
]
