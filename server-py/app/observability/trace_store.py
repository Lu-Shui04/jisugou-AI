"""全链路追踪的库表与查询

写入在 app/observability/trace.py（一次请求一个事务写 run + steps）；
这里只负责：建表 + 管理后台要用的四个查询（列表 / 小结 / 详情 / 清空）。

表结构（两条）：
    trace_runs    一次请求一行：谁问的、走的哪个入口、多久、几步、成功还是失败
    trace_steps   一个步骤一行：什么时候（+偏移）、哪一类、叫什么、耗时、入参出参

为什么不用 Redis（现在对话记录用的是 Redis）：追踪要能按"问题关键词 / 入口 / 状态"翻、
要能对着某个 run_id 深链回来、还要按时间线排序 —— 这些是关系库的活；
对话记录的"最近 N 条时间线"才是 Redis 的活。两者并存，run_id 是同一个。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("jisu.trace")

# 建表语句逐条执行（而不是一整个多语句块）：任何一条失败不会把前面建好的表一起回滚
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
CREATE TABLE IF NOT EXISTS trace_runs (
    run_id      TEXT PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    feature     TEXT NOT NULL,
    question    TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    user_name   TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    step_count  INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    summary     JSONB NOT NULL DEFAULT '{}'::jsonb
)
""",
    "CREATE INDEX IF NOT EXISTS idx_trace_runs_ts      ON trace_runs (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trace_runs_feature ON trace_runs (feature, ts DESC)",
    """
CREATE TABLE IF NOT EXISTS trace_steps (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES trace_runs(run_id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    detail      JSONB NOT NULL DEFAULT '{}'::jsonb
)
""",
    # 唯一索引：一次请求的同一步骤只能有一条 —— 收尾逻辑万一被调用两次，
    # 落库用 ON CONFLICT DO NOTHING 挡住，时间线不会被写重（实测踩到过：重复收尾后
    # 步骤数从 7 变 8，页面上看就是"这一步执行了两遍"）
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_trace_steps_run_idx ON trace_steps (run_id, idx)",
)


def _decode(value):
    """asyncpg 取 jsonb 回来是字符串（没注册 codec），统一解成对象"""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


async def init_schema(pool) -> bool:
    """建表（幂等）。库不可用/建表失败只记日志：追踪不该拦住服务启动"""
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            for statement in SCHEMA_STATEMENTS:
                await conn.execute(statement)
        logger.info("全链路追踪表已就绪（trace_runs / trace_steps）")
        return True
    except Exception as err:  # noqa: BLE001
        logger.warning("全链路追踪建表失败（该功能将不可用）：%s", err)
        return False


def _run_row(row, labels: dict) -> dict:
    return {
        "runId": row["run_id"],
        "time": row["ts"].isoformat(),
        "feature": row["feature"],
        "featureLabel": labels.get(row["feature"], row["feature"]),
        "question": row["question"],
        "userId": row["user_id"],
        "userName": row["user_name"],
        "sessionId": row["session_id"],
        "status": row["status"],
        "durationMs": int(row["duration_ms"] or 0),
        "stepCount": int(row["step_count"] or 0),
        "error": row["error"] or "",
        "summary": _decode(row["summary"]),
    }


async def list_runs(pool, labels: dict, *, limit: int = 50, offset: int = 0,
                    feature: str = "", status: str = "", keyword: str = "") -> dict:
    """追踪列表（不带步骤，步骤由详情接口给）"""
    where = ["1=1"]
    args: list = []
    if feature:
        args.append(feature)
        where.append(f"feature = ${len(args)}")
    if status:
        args.append(status)
        where.append(f"status = ${len(args)}")
    if keyword:
        args.append(f"%{keyword}%")
        where.append(f"(question ILIKE ${len(args)} OR run_id ILIKE ${len(args)}"
                     f" OR user_id ILIKE ${len(args)} OR user_name ILIKE ${len(args)})")
    clause = " AND ".join(where)
    page_args = args + [limit, offset]

    sql = f"""
        SELECT run_id, ts, feature, question, user_id, user_name, session_id,
               status, duration_ms, step_count, error, summary
        FROM trace_runs
        WHERE {clause}
        ORDER BY ts DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
    """
    count_sql = f"SELECT count(*) FROM trace_runs WHERE {clause}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *page_args)
        total = await conn.fetchval(count_sql, *args)
    return {"total": int(total or 0), "runs": [_run_row(row, labels) for row in rows]}


async def get_run(pool, labels: dict, run_id: str) -> Optional[dict]:
    """一条追踪的完整时间线：每一步的类型 / 耗时 / 状态 / 入参出参"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM trace_runs WHERE run_id = $1", run_id)
        if row is None:
            return None
        steps = await conn.fetch(
            "SELECT idx, ts, kind, name, status, duration_ms, detail "
            "FROM trace_steps WHERE run_id = $1 ORDER BY idx",
            run_id,
        )
    data = _run_row(row, labels)
    data["steps"] = [
        {
            "idx": int(step["idx"]),
            "time": step["ts"].isoformat(),
            "offsetMs": int((step["ts"] - row["ts"]).total_seconds() * 1000),
            "kind": step["kind"],
            "name": step["name"],
            "status": step["status"],
            "durationMs": int(step["duration_ms"] or 0),
            "detail": _decode(step["detail"]),
        }
        for step in steps
    ]
    return data


async def stats(pool, labels: dict) -> dict:
    """页面顶部小结：今天各入口跑了多少次、有没有失败"""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT feature,
                   count(*)                                              AS total,
                   -- 只把 error / blocked 算失败：running 是"还没跑完"，
                   -- 算进失败会让人以为出事了（页面同时显示"失败 1 / 进行中 1"很吓人）
                   count(*) FILTER (WHERE status IN ('error', 'blocked')) AS failed,
                   count(*) FILTER (WHERE status = 'running')            AS running
            FROM trace_runs
            WHERE ts >= date_trunc('day', now())
            GROUP BY feature ORDER BY total DESC
            """
        )
        total = await conn.fetchval("SELECT count(*) FROM trace_runs")
    return {
        "totalRuns": int(total or 0),
        "today": [
            {
                "feature": row["feature"],
                "label": labels.get(row["feature"], row["feature"]),
                "total": int(row["total"]),
                "failed": int(row["failed"]),
                "running": int(row["running"]),
            }
            for row in rows
        ],
    }


async def clear_runs(pool) -> int:
    """清空全部追踪（联调时用，避免旧数据干扰判断）——不动对话记录"""
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM trace_runs")
        await conn.execute("TRUNCATE trace_runs CASCADE")
    return int(count or 0)
