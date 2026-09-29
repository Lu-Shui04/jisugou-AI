"""全链路追踪接口（管理后台「链路追踪」页签用）

一次请求 = 一条 run，请求里的每个环节 = 一条 step。数据落在 PostgreSQL 的
trace_runs / trace_steps（写入见 app/observability/trace.py，表结构见 trace_store.py）。

鉴权与其它后台接口一致：复用 admin.require_admin。
库不可用时返回 503，而不是把空列表当成"没有记录"——这两件事对排查的含义完全不同。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.postgres import get_pool
from app.observability import trace_store
from app.observability.trace import RETENTION_DAYS, ROUTE_LABELS, TRACE_ENABLED
from app.routers.admin import require_admin

logger = logging.getLogger("jisu.trace")

router = APIRouter()


def _require_pool():
    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL 不可用，全链路追踪暂不可用（业务请求不受影响）",
        )
    return pool


@router.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    feature: str = Query("", description="入口：chat / agent / rag / graph"),
    status: str = Query("", description="状态：ok / error / blocked / running"),
    q: str = Query("", description="按问题 / run_id / 用户搜"),
    admin: dict = Depends(require_admin),
):
    """追踪列表（不含步骤，步骤由详情接口给）"""
    return await trace_store.list_runs(
        _require_pool(), ROUTE_LABELS,
        limit=limit, offset=offset, feature=feature.strip(), status=status.strip(),
        keyword=q.strip(),
    )


@router.get("/stats")
async def stats(admin: dict = Depends(require_admin)):
    """页面顶部小结：今天各入口跑了多少次、有没有失败"""
    data = await trace_store.stats(_require_pool(), ROUTE_LABELS)
    data["enabled"] = TRACE_ENABLED
    data["retentionDays"] = RETENTION_DAYS
    return data


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, admin: dict = Depends(require_admin)):
    """一条追踪的完整时间线：每一步的类型 / 耗时 / 状态 / 入参出参"""
    data = await trace_store.get_run(_require_pool(), ROUTE_LABELS, run_id)
    if data is None:
        raise HTTPException(status_code=404, detail="追踪记录不存在（可能已过保留期被清理）")
    return data


@router.delete("/runs")
async def clear_runs(admin: dict = Depends(require_admin)):
    """清空全部追踪记录（**不动对话记录**）"""
    cleared = await trace_store.clear_runs(_require_pool())
    logger.info("清空全链路追踪：%d 条", cleared)
    return {"success": True, "cleared": cleared}
