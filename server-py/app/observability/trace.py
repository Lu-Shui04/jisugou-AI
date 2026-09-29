"""全链路追踪：一次请求内部到底发生了什么，按时间顺序记下来

为什么要有它
------------
排查问题原来只能 `docker logs | grep`：一次对话几十行、跑完就翻不到，也没法对着
"某一条聊天记录"回看它当时走了哪条路。用户只看得到最后那句话，而
"这次为什么没查知识库 / 工具参数错在哪 / 这 8 秒花在哪" 全散在日志碎片里。

所以给每个请求起一条 trace：**run（一次请求）+ step（一个步骤）**，落 PostgreSQL，
管理后台「链路追踪」页签按时间线展开，每一步的入参出参都能点开看。

设计要点（每一条都是踩过坑才这么写的）
--------------------------------------
1. **ContextVar 传递**：入口 start_trace() 之后，调用链深处（安全判定 / 接力判定 /
   检索 / 重排 / 工具 / 模型）直接 trace_step(...) 就行，不用把 trace 对象一层层塞进函数签名。
2. **缓冲 + 一次落库**：步骤先放内存，finish() 时一个事务写 run + 全部 step。
   追踪绝不能拖慢正常请求（尤其 SSE 流式），更不能因为写库失败把业务带崩。
3. **截断**：detail 里的长文本（回答全文、命中的原文片段、提示词）按上限截断，
   避免一条 trace 几百 KB 把库撑爆；截断处会写明"原文共多少字"，不假装数据是完整的。
4. **没有追踪时零成本**：current_trace() 为 None 时所有 trace_step 都是空操作；
   连不上库时 start_trace 之后的一切照常，只是不落库。
5. **run_id 复用对话记录的 trace_id**：管理后台「对话记录」和「链路追踪」看的是同一个 id，
   两边可以互相跳转（点一条聊天记录直接看它当时怎么跑的）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger("jisu.trace")

# 总开关（关掉以后 start_trace 返回 None，全部埋点变成空操作）
TRACE_ENABLED = os.getenv("TRACE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# 单个字符串字段的字符上限（回答 / 提示词 / 片段原文都会走这里）
MAX_STR = int(os.getenv("TRACE_MAX_STR", "2000"))
# 单步 detail 序列化后的字节上限，超了只留预览
MAX_DETAIL_BYTES = int(os.getenv("TRACE_MAX_DETAIL", "16000"))
# 单个 run 最多记多少步（跑飞的循环不至于把内存和库撑爆）
MAX_STEPS = int(os.getenv("TRACE_MAX_STEPS", "300"))
# 保留天数：超期的 trace 在 finish 时顺手清掉（ts 上有索引，代价很低）
RETENTION_DAYS = int(os.getenv("TRACE_RETENTION_DAYS", "7"))
# 单次落库的时间上限（秒）：追踪写在请求收尾的关键路径上，
# 数据库慢/池被打满时宁可丢这一次追踪，也不能让用户多等
WRITE_TIMEOUT_SECONDS = float(os.getenv("TRACE_WRITE_TIMEOUT_SECONDS", "3"))

# 入口 → 展示名（管理后台的筛选下拉直接用这个）
# 文案与前端管理后台保持完全一致（AdminView 的 ROUTE_LABELS 用的是这一套），
# 否则同一个入口在两个页签里会有两种叫法
ROUTE_LABELS = {
    "chat": "基础对话",
    "agent": "订单查询",
    "rag": "知识库问答",
    "graph": "智能中枢",
}

# 阶段名 → 步骤类型（决定前端显示哪个图标、哪一组颜色）
# 没有映射的阶段一律落成 "step"，前端有兜底图标，不会显示不出来。
STAGE_KINDS = {
    "identity": "identity",
    "input": "input",
    "guard": "guard",
    "blocked": "blocked",
    "identity_required": "blocked",
    "handoff": "handoff",
    "session": "session",
    "output": "output",
    "grounding": "grounding",
    # 接地校验拦下编造回答：这是"策略拦住了"，不是程序崩了 —— 归到 grounding 这一层
    # （状态仍是 error，时间线上红点 + 接地图标，和真异常区分开）
    "grounding_blocked": "grounding",
    "intent": "intent",
    "node": "node",
    "sources": "sources",
    "tool": "tool",
    "tool_result": "tool_result",
    "answer": "answer",
    "retrieval": "retrieval",
    "rerank": "rerank",
}


def _clip(value, depth: int = 0):
    """递归裁剪：长字符串截断、超长列表/字典只留前若干项"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= MAX_STR:
            return value
        return value[:MAX_STR] + f"…（已截断，原文共 {len(value)} 字）"
    if depth >= 6:
        return "…（层级过深，已省略）"
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 60:
                out["…"] = f"另有 {len(value) - 60} 个字段"
                break
            out[str(key)] = _clip(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [_clip(item, depth + 1) for item in items[:60]]
        if len(items) > 60:
            out.append(f"…（另有 {len(items) - 60} 项）")
        return out
    return _clip(str(value), depth + 1)


def _pack_detail(detail) -> dict:
    """裁剪 + 兜底：detail 必须是能存进 JSONB 的字典"""
    if detail is None:
        return {}
    if not isinstance(detail, dict):
        detail = {"value": detail}
    clipped = _clip(detail)
    try:
        text = json.dumps(clipped, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 无法序列化时也不能让"记录"本身报错
        return {"note": "detail 无法序列化", "repr": _clip(str(detail))}
    if len(text.encode("utf-8")) > MAX_DETAIL_BYTES:
        return {
            "truncated": True,
            "bytes": len(text.encode("utf-8")),
            "preview": text[: MAX_DETAIL_BYTES // 2],
        }
    return clipped


class Trace:
    """一次请求的追踪上下文"""

    def __init__(self, feature: str, run_id: str = "", question: str = "",
                 user_id: str = "", user_name: str = "", session_id: str = "",
                 meta: Optional[dict] = None):
        self.run_id = run_id or ("tr_" + uuid.uuid4().hex[:12])
        self.feature = feature
        self.question = question or ""
        self.user_id = user_id or ""
        self.user_name = user_name or ""
        self.session_id = session_id or ""
        self.started = time.time()
        self.steps: list[dict] = []
        self.status = "running"
        self.error = ""
        self.summary: dict = dict(meta or {})
        self._finished = False
        self._dropped = 0

    # ── 记步骤 ────────────────────────────────────────────────────
    def step(self, kind: str, name: str, *, status: str = "ok",
             detail=None, duration_ms: int = 0, at: Optional[float] = None) -> None:
        """记一步。

        at 传"这一步真正发生的时间"（epoch 秒）：模型调用、检索这类明细是在回调里
        采集的，只能等收尾时统一补记 —— 不带上真实时间的话，时间线上会看到
        "模型调用"排在"返回回答"后面，顺序是错的。
        """
        if self._finished:
            return
        if len(self.steps) >= MAX_STEPS:
            self._dropped += 1
            return
        self.steps.append({
            "idx": len(self.steps),
            "offset_ms": int(((at or time.time()) - self.started) * 1000),
            "kind": kind or "step",
            "name": name,
            "status": status,
            "duration_ms": int(duration_ms or 0),
            "detail": _pack_detail(detail),
        })

    def span(self, kind: str, name: str, detail=None):
        """with / async with 都能用的计时片段：退出时自动记耗时与异常"""
        return _Span(self, kind, name, detail)

    def fail(self, name: str, err: BaseException, detail=None) -> None:
        self.error = f"{type(err).__name__}: {err}"
        self.step("error", name, status="error", detail={"error": self.error, **(detail or {})})

    # ── 收尾落库 ──────────────────────────────────────────────────
    async def finish(self, *, status: str = "ok", summary: Optional[dict] = None,
                     error: Optional[str] = None) -> str:
        if self._finished:
            return self.run_id
        self._finished = True
        self.status = status
        if error:
            self.error = error
        if summary:
            self.summary.update(summary)
        if self._dropped:
            self.summary["droppedSteps"] = self._dropped
        duration_ms = int((time.time() - self.started) * 1000)

        from app.db.postgres import get_pool

        pool = get_pool()
        if pool is None:
            # 没库（或库挂了）时追踪静默失效：绝不能因为"记日志"把业务带崩
            logger.debug("trace: 数据库不可用，本次追踪未落库 run_id=%s", self.run_id)
            return self.run_id
        try:
            await asyncio.wait_for(self._persist(pool, duration_ms), timeout=WRITE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("trace: 落库超时（%.1fs），本次追踪丢弃 run_id=%s",
                           WRITE_TIMEOUT_SECONDS, self.run_id)
        except Exception as err:  # noqa: BLE001
            logger.warning("trace: 落库失败 run_id=%s：%s", self.run_id, err)
        return self.run_id

    async def _persist(self, pool, duration_ms: int) -> None:
        """真正写库的部分：一个事务写 run + 全部 step，顺带清理过期数据"""
        # 按"真正发生的时间"排好序再落库，并按时间线重新编号：
        # 补记的步骤（模型调用 / 检索明细）可能比它后面的阶段晚入队，
        # 不排序的话前端时间线顺序就是错的（会看到"模型调用"排在"返回回答"之后）。
        ordered = sorted(self.steps, key=lambda item: (item["offset_ms"], item["idx"]))
        for index, item in enumerate(ordered):
            item["idx"] = index
        async with pool.acquire() as conn:
            async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO trace_runs
                            (run_id, ts, feature, question, user_id, user_name, session_id,
                             status, duration_ms, step_count, error, summary)
                        VALUES ($1, to_timestamp($2), $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        self.run_id, self.started, self.feature, _clip(self.question),
                        self.user_id, self.user_name, self.session_id,
                        self.status, duration_ms, len(self.steps), self.error or None,
                        json.dumps(_clip(self.summary), ensure_ascii=False, default=str),
                    )
                    if self.steps:
                        await conn.executemany(
                            """
                            INSERT INTO trace_steps
                                (run_id, idx, ts, kind, name, status, duration_ms, detail)
                            VALUES ($1, $2, to_timestamp($3), $4, $5, $6, $7, $8::jsonb)
                            ON CONFLICT (run_id, idx) DO NOTHING
                            """,
                            [(self.run_id, item["idx"],
                              self.started + item["offset_ms"] / 1000.0,
                              item["kind"], item["name"], item["status"], item["duration_ms"],
                              json.dumps(item["detail"], ensure_ascii=False, default=str))
                             for item in ordered],
                        )
                    # 保留策略：顺手清掉过期的（trace_runs.ts 上有索引）
                    if RETENTION_DAYS > 0:
                        await conn.execute(
                            "DELETE FROM trace_runs WHERE ts < now() - ($1 || ' days')::interval",
                            str(RETENTION_DAYS),
                        )


class _Span:
    """计时片段：with / async with 都能用

    detail 可以在片段内部继续补充（比如检索完把命中数写进去），退出时统一记录；
    片段内抛异常则自动记成 error 步骤。
    """

    def __init__(self, trace: Trace, kind: str, name: str, detail=None):
        self.trace = trace
        self.kind = kind
        self.name = name
        self.detail = dict(detail or {})
        self.started = time.time()
        self._done = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._close(exc)
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._close(exc)
        return False

    def _close(self, exc) -> None:
        if self._done:
            return
        self._done = True
        duration = int((time.time() - self.started) * 1000)
        if exc is not None:
            self.detail["error"] = f"{type(exc).__name__}: {exc}"
            self.trace.step(self.kind, self.name, status="error",
                            detail=self.detail, duration_ms=duration)
        else:
            self.trace.step(self.kind, self.name, detail=self.detail, duration_ms=duration)


class _NullSpan:
    """没有追踪上下文时的占位片段：能当 with / async with 用，什么都不记"""

    def __init__(self, kind: str, name: str, detail=None):
        self.kind, self.name = kind, name
        self.detail = dict(detail or {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


_current: ContextVar[Optional[Trace]] = ContextVar("jisu_trace", default=None)


def start_trace(feature: str, run_id: str = "", question: str = "", user_id: str = "",
                user_name: str = "", session_id: str = "", meta: Optional[dict] = None
                ) -> Optional[Trace]:
    """开一条追踪并设为当前上下文（入口处调用一次）。

    TRACE_ENABLED=false 时返回 None，后续所有埋点自动变成空操作。
    """
    if not TRACE_ENABLED:
        return None
    trace = Trace(feature, run_id=run_id, question=question, user_id=user_id,
                  user_name=user_name, session_id=session_id, meta=meta)
    _current.set(trace)
    return trace


def current_trace() -> Optional[Trace]:
    return _current.get()


def trace_step(kind: str, name: str, *, status: str = "ok",
               detail=None, duration_ms: int = 0) -> None:
    """在任意调用链深处记一步（没有追踪上下文时是空操作）"""
    trace = _current.get()
    if trace is not None:
        trace.step(kind, name, status=status, detail=detail, duration_ms=duration_ms)


def trace_span(kind: str, name: str, detail=None):
    """计时片段；没有追踪上下文时返回空占位（with / async with 都能用）"""
    trace = _current.get()
    if trace is None:
        return _NullSpan(kind, name, detail)
    return trace.span(kind, name, detail)


def stage_kind(name: str) -> str:
    """阶段名 → 步骤类型（前端按类型给图标与配色）"""
    return STAGE_KINDS.get((name or "").strip(), "step")


async def finish_trace(*, status: str = "ok", summary: Optional[dict] = None,
                       error: Optional[str] = None) -> Optional[str]:
    """收尾当前追踪（没有追踪时返回 None）；落库失败也不会抛异常"""
    trace = _current.get()
    if trace is None:
        return None
    return await trace.finish(status=status, summary=summary, error=error)
