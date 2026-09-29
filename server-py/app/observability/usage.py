"""Token 消耗统计 + 对话审计采集

每次请求创建一个 RequestUsage：
- 挂到链路的 callbacks 上，Chain / Agent / LangGraph 内部的每次 LLM 调用都会被累计
- RAG 检索的命中片段与重排（相似度打分 + 阈值过滤）明细通过 record_retrieval() 记进来
- 请求结束时 finish() 输出一行结构化日志、按维度累计到 Redis，并写入一条对话记录
- 返回值随 SSE 的 usage 事件推给前端

Redis 结构：
    usage:{YYYY-MM-DD}        HASH  当日汇总（requests / tokens / errors / latency_ms）
    usage:total               HASH  累计汇总
    usage:route:{route}       HASH  按入口汇总
    usage:model:{model}       HASH  按模型汇总
    usage:user:{user_id}      HASH  按用户汇总
    usage:days                ZSET  有数据的日期
    usage:day_users:{day}     ZSET  当日活跃用户（ZCARD 即活跃用户数）
    usage:users               ZSET  有调用记录的用户

Redis 不可用时只打日志 + 写内存兜底，不影响请求。
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler

from app.db import redis_client
from app.observability import chatlog
from app.observability import trace as trace_mod

logger = logging.getLogger("jisu.usage")

# 用量统计数据保留时间，默认 7 天
USAGE_RETENTION_SECONDS = int(os.getenv("USAGE_RETENTION_SECONDS", str(7 * 24 * 3600)))
# 单轮记录里问答文本的最大长度（防止超长回答撑爆记录）
MAX_TEXT_CHARS = int(os.getenv("CHATLOG_MAX_TEXT_CHARS", "4000"))
# 单轮记录里保留的检索片段数
MAX_RETRIEVAL_HITS = int(os.getenv("CHATLOG_MAX_RETRIEVAL_HITS", "10"))
# 排行榜取前 N 名
USAGE_TOP_N = int(os.getenv("USAGE_TOP_N", "10"))
# 全链路追踪：单次请求最多记录多少次模型调用、提示词预览截断长度
MAX_LLM_CALLS = int(os.getenv("TRACE_MAX_LLM_CALLS", "20"))
MAX_PROMPT_PREVIEW = int(os.getenv("TRACE_PROMPT_PREVIEW_CHARS", "400"))

_COUNTER_FIELDS = ("requests", "prompt_tokens", "completion_tokens", "total_tokens",
                   "latency_ms", "errors", "blocked")


def _pick(usage: dict, *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _normalize(usage: dict) -> dict:
    prompt = _pick(usage, "input_tokens", "prompt_tokens")
    completion = _pick(usage, "output_tokens", "completion_tokens")
    total = _pick(usage, "total_tokens") or prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _extract_usage(response) -> Optional[dict]:
    """从 LLMResult 提取 token 用量

    - 非流式：llm_output.token_usage
    - 流式：最终 chunk 的 message.usage_metadata（需 ChatOpenAI 开 stream_usage）
    """
    llm_output = getattr(response, "llm_output", None) or {}
    for key in ("token_usage", "usage"):
        usage = llm_output.get(key)
        if isinstance(usage, dict) and usage:
            return _normalize(usage)

    for generation_list in getattr(response, "generations", None) or []:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "usage_metadata", None)
            if isinstance(metadata, dict) and metadata:
                return _normalize(metadata)
    return None


def _clip(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


class TokenUsageCallback(BaseCallbackHandler):
    """累计一次请求内所有 LLM 调用的 token 消耗，并收集检索/重排明细"""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.llm_calls = 0
        self.models: list[str] = []
        self.retrievals: list[dict] = []
        # 全链路追踪用：每次模型调用的耗时 / token / 提示词预览
        self.calls: list[dict] = []
        self._started_at: Optional[float] = None
        self._prompt_preview: str = ""

    def _begin_call(self, prompt_text: str) -> None:
        self._started_at = time.perf_counter()
        self._prompt_preview = _clip(prompt_text, MAX_PROMPT_PREVIEW)

    def on_chat_model_start(self, serialized, messages, **kwargs: Any) -> None:
        """Chat 模型走这个钩子（拿到的是消息列表，取最后一条做预览）"""
        try:
            last = messages[-1][-1] if messages and messages[-1] else None
            text = getattr(last, "content", "") or ""
        except Exception:
            text = ""
        self._begin_call(str(text))

    def on_llm_start(self, serialized, prompts, **kwargs: Any) -> None:
        self._begin_call((prompts or [""])[-1] if prompts else "")

    def on_llm_end(self, response, **kwargs: Any) -> None:
        usage = _extract_usage(response)
        model = (getattr(response, "llm_output", None) or {}).get("model_name") or ""
        latency = int((time.perf_counter() - self._started_at) * 1000) if self._started_at else 0

        if usage:
            self.prompt_tokens += usage["prompt_tokens"]
            self.completion_tokens += usage["completion_tokens"]
            self.total_tokens += usage["total_tokens"]
            self.llm_calls += 1
            if model and model not in self.models:
                self.models.append(model)

        # 不管有没有 usage 都留一条调用记录（链路里能看到"模型被调了几次、每次多久"）
        if len(self.calls) < MAX_LLM_CALLS:
            self.calls.append({
                "model": model or (self.models[0] if self.models else ""),
                "prompt_tokens": usage["prompt_tokens"] if usage else 0,
                "completion_tokens": usage["completion_tokens"] if usage else 0,
                "total_tokens": usage["total_tokens"] if usage else 0,
                "latency_ms": latency,
                "prompt_preview": self._prompt_preview,
            })
        self._started_at = None
        self._prompt_preview = ""

    def record_retrieval(
        self,
        query: str = "",
        top_k: int = 0,
        threshold: float = 0.0,
        candidates: Optional[list] = None,
        kept: int = 0,
        filtered: bool = False,
        degraded: bool = False,
        error: str = "",
        rerank: str = "",
        embedded_query: str = "",
        rerank_stats: Optional[dict] = None,
    ) -> None:
        """记录一次「知识库检索 + 重排」的完整明细

        candidates: [{"source": str, "score": float, "content": str, "kept": bool}]
        rerank:     重排策略说明（默认阈值过滤；开启重排后是硅基流动 bge-reranker-v2-m3）
        """
        if len(self.retrievals) >= 5:  # 单轮最多留 5 次检索（多节点工作流可能检索多次）
            return
        self.retrievals.append(
            {
                "query": _clip(query, 500),
                # 实际拿去向量化的查询（口语化问句会被归一化，如"耳机咋卖"→"耳机"）
                "embedded_query": _clip(embedded_query or query, 500),
                "top_k": int(top_k or 0),
                "threshold": float(threshold or 0.0),
                "rerank": rerank or "阈值过滤 + 关键词召回 + RRF 融合",
                # 重排（硅基流动 bge-reranker-v2-m3，或关闭时的本地策略）的统计与"重排前后名次"
                "rerank_stats": rerank_stats or {},
                "kept": int(kept or 0),
                "filtered": bool(filtered),
                "degraded": bool(degraded),
                "error": _clip(error, 300),
                "hits": [
                    {
                        "source": _clip((hit or {}).get("source"), 200),
                        "score": round(float((hit or {}).get("score") or 0.0), 4),
                        "kept": bool((hit or {}).get("kept")),
                        "match": (hit or {}).get("match") or "vector",
                        "rank_after": (hit or {}).get("rank_after"),
                        "rerank_score": (hit or {}).get("rerank_score"),
                        "content": _clip((hit or {}).get("content"), 200),
                    }
                    for hit in (candidates or [])[:MAX_RETRIEVAL_HITS]
                ],
            }
        )

    @property
    def model_name(self) -> str:
        return self.models[0] if self.models else ""


def find_usage_callback(config) -> Optional[TokenUsageCallback]:
    """从 LangChain config 的 callbacks 里找出本次请求的采集器"""
    if not config:
        return None
    callbacks = config.get("callbacks") if isinstance(config, dict) else getattr(config, "callbacks", None)
    if callbacks is None:
        return None
    handlers = getattr(callbacks, "handlers", None)
    if handlers is None:
        handlers = callbacks if isinstance(callbacks, (list, tuple, set)) else [callbacks]
    for handler in handlers:
        if isinstance(handler, TokenUsageCallback):
            return handler
    return None


def record_retrieval(config, **kwargs: Any) -> None:
    """供 RAG 链路调用：把检索/重排明细挂到本次请求的采集器上"""
    callback = find_usage_callback(config)
    if callback is None:
        return
    try:
        callback.record_retrieval(**kwargs)
    except Exception as err:  # 明细记录失败不能影响主流程
        logger.warning("记录检索明细失败: %s", err)


class RequestUsage:
    """一次请求的 token / 耗时 / 问答内容采集器"""

    def __init__(self, route: str, session_id: str = "", model: str = "",
                 user_id: str = "", user_name: str = "") -> None:
        self.trace_id = uuid.uuid4().hex[:16]
        self.route = route
        self.session_id = session_id or ""
        self.model = model or os.getenv("MODEL_NAME", "deepseek-chat")
        self.user_id = (user_id or "").strip()
        self.user_name = (user_name or "").strip()
        # 全链路追踪：run_id 直接用 trace_id —— 后台「对话记录」和「链路追踪」
        # 看的是同一次请求，两边可以互相点进去对照
        self.trace = trace_mod.start_trace(
            route, run_id=self.trace_id, user_id=self.user_id,
            user_name=self.user_name, session_id=self.session_id,
        )
        self.callback = TokenUsageCallback()
        self.question = ""
        self.answer = ""
        self.steps: list = []
        self.error = ""
        # 全链路追踪：按发生顺序记录每个阶段的所见所为
        self.stages: list[dict] = []
        self.started_at = time.perf_counter()

    @property
    def callbacks(self) -> list:
        return [self.callback]

    @property
    def retrievals(self) -> list:
        return self.callback.retrievals

    def set_question(self, question: str) -> None:
        self.question = _clip(question)
        if self.trace is not None:
            self.trace.question = self.question

    def set_answer(self, answer: str) -> None:
        self.answer = _clip(answer)

    def set_steps(self, steps: Optional[list]) -> None:
        self.steps = list(steps or [])[:20]

    def set_error(self, error: Any) -> None:
        self.error = _clip(error, 500)

    def stage(self, name: str, **detail: Any) -> None:
        """记一个链路阶段（管理员后台「链路追踪」按顺序展示）

        name 用短标识：input / guard / session / intent / retrieval / tool / llm / output / answer …
        """
        if len(self.stages) >= 80:
            return
        self.stages.append({
            "name": name,
            "at_ms": self.elapsed_ms(),
            "ts": int(time.time() * 1000),
            **{key: (_clip(value) if isinstance(value, str) else value) for key, value in detail.items()},
        })
        # 同一份阶段信息同时写进全链路追踪：管理后台「链路追踪」页签按时间线展开，
        # 每一步都能点开看入参出参（stage 已经是"哪一层、看到了什么"的现成结构）
        if self.trace is not None:
            self.trace.step(
                trace_mod.stage_kind(name), name,
                status="error" if name in ("blocked", "grounding_blocked") else "ok",
                detail=detail,
            )

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)

    # ── 全链路追踪：把回调采集到的明细补成步骤 ──────────────────────
    def _record_trace_details(self) -> None:
        """把「每次模型调用」与「每次检索 / 重排」补成追踪步骤

        这两类明细是 LangChain 回调在**别的线程**里采集的（LangGraph 的节点跑在线程池，
        ContextVar 不跨线程传播），埋点写在回调里会丢；统一在主流程收尾时补记，
        顺序稳定、也不会漏掉关键信息。
        """
        trace = self.trace
        if trace is None:
            return

        for index, call in enumerate(self.callback.calls, start=1):
            model = call.get("model") or self.callback.model_name or self.model
            trace.step(
                "llm", f"模型调用 #{index}（{model}）",
                duration_ms=int(call.get("latency_ms") or 0),
                detail={
                    "model": model,
                    "prompt_tokens": int(call.get("prompt_tokens") or 0),
                    "completion_tokens": int(call.get("completion_tokens") or 0),
                    "total_tokens": int(call.get("total_tokens") or 0),
                    "prompt_preview": call.get("prompt_preview") or "",
                },
            )

        for index, item in enumerate(self.callback.retrievals, start=1):
            query = item.get("query") or ""
            trace.step(
                "retrieval", f"知识库检索 #{index}：{query[:60]}",
                status="error" if item.get("error") else "ok",
                detail={
                    "query": query,
                    "embedded_query": item.get("embedded_query") or "",
                    "top_k": item.get("top_k"),
                    "threshold": item.get("threshold"),
                    "kept": item.get("kept"),
                    "filtered": item.get("filtered"),
                    "degraded": item.get("degraded"),
                    "error": item.get("error") or "",
                    "rerank": item.get("rerank") or "",
                    "hits": item.get("hits") or [],
                },
            )
            stats = item.get("rerank_stats") or {}
            if stats:
                trace.step(
                    "rerank", f"重排 #{index}：{stats.get('model') or stats.get('provider') or '本地策略'}",
                    detail=stats,
                )

    async def _finish_trace(self, status: str) -> None:
        """收尾追迹：补明细 → 记一条 response → 一次事务落库（失败只记日志）"""
        trace = self.trace
        if trace is None:
            return
        self._record_trace_details()
        trace.step(
            "response", "返回回答",
            status="error" if status == "error" else "ok",
            detail={
                "status": status,
                "answer": self.answer,
                "latency_ms": self.elapsed_ms(),
                "model": self.callback.model_name or self.model,
                "total_tokens": self.callback.total_tokens,
                "llm_calls": self.callback.llm_calls,
            },
        )
        await trace.finish(
            status={"ok": "ok", "blocked": "blocked"}.get(status, "error"),
            summary={
                "route": self.route,
                "sessionId": self.session_id,
                "model": self.callback.model_name or self.model,
                "promptTokens": self.callback.prompt_tokens,
                "completionTokens": self.callback.completion_tokens,
                "totalTokens": self.callback.total_tokens,
                "llmCalls": self.callback.llm_calls,
                "retrievalCount": len(self.callback.retrievals),
            },
            error=self.error or None,
        )

    def _record(self, status: str) -> dict:
        """完整的对话记录（落 Redis + 内存，供管理员后台审计）"""
        now = datetime.now(timezone.utc)
        return {
            "trace_id": self.trace_id,
            "ts": int(now.timestamp() * 1000),
            "date": now.strftime("%Y-%m-%d"),
            "route": self.route,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "session_id": self.session_id,
            "question": self.question,
            "answer": self.answer,
            "model": self.callback.model_name or self.model,
            "prompt_tokens": self.callback.prompt_tokens,
            "completion_tokens": self.callback.completion_tokens,
            "total_tokens": self.callback.total_tokens,
            "llm_calls": self.callback.llm_calls,
            "latency_ms": self.elapsed_ms(),
            "status": status,
            "error": self.error,
            "steps": self.steps,
            "retrievals": self.callback.retrievals,
            # 全链路：阶段时间线 + 每次模型调用明细（模型名缺失时用本次请求的模型兜底）
            "stages": self.stages,
            "llm_calls_detail": [
                {**call, "model": call.get("model") or (self.callback.model_name or self.model)}
                for call in self.callback.calls
            ],
        }

    async def finish(self, status: str = "ok") -> dict:
        record = self._record(status)
        payload = {
            "trace_id": record["trace_id"],
            "route": record["route"],
            "session_id": record["session_id"],
            "user_id": record["user_id"],
            "user_name": record["user_name"],
            "model": record["model"],
            "prompt_tokens": record["prompt_tokens"],
            "completion_tokens": record["completion_tokens"],
            "total_tokens": record["total_tokens"],
            "llm_calls": record["llm_calls"],
            "latency_ms": record["latency_ms"],
            "status": record["status"],
            "retrieval_count": len(record["retrievals"]),
        }
        # 每次请求一行结构化日志
        logger.info("usage %s", json.dumps(payload, ensure_ascii=False))
        await self._finish_trace(status)
        await _accumulate(record)
        await chatlog.record_turn(record)
        return payload


# ── 用量累计 ────────────────────────────────────────────────────
_memory_usage: dict[str, dict] = {}


def _memory_incr(key: str, record: dict) -> None:
    bucket = _memory_usage.setdefault(key, {field: 0 for field in _COUNTER_FIELDS})
    bucket["requests"] += 1
    bucket["prompt_tokens"] += record["prompt_tokens"]
    bucket["completion_tokens"] += record["completion_tokens"]
    bucket["total_tokens"] += record["total_tokens"]
    bucket["latency_ms"] += record["latency_ms"]
    # 安全拦截不算系统错误，分开计数：否则"错误率"会虚高（实测 16.67% 里 100% 是拦截）
    if record.get("status") == "blocked":
        bucket["blocked"] += 1
    elif record.get("status") != "ok":
        bucket["errors"] += 1


async def _accumulate(record: dict) -> None:
    """按天 / 入口 / 模型 / 用户累计到 Redis，供用量看板查询"""
    day = record["date"]
    keys = ["usage:total", f"usage:{day}", f"usage:route:{record['route']}"]
    if record.get("model"):
        keys.append(f"usage:model:{record['model']}")
    if record.get("user_id"):
        keys.append(f"usage:user:{record['user_id']}")

    try:
        client = redis_client.get_redis()
        async with client.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.hincrby(key, "requests", 1)
                pipe.hincrby(key, "prompt_tokens", record["prompt_tokens"])
                pipe.hincrby(key, "completion_tokens", record["completion_tokens"])
                pipe.hincrby(key, "total_tokens", record["total_tokens"])
                pipe.hincrby(key, "latency_ms", record["latency_ms"])
                # 拦截与失败分开统计：拦截是安全策略生效，不是系统故障
                pipe.hincrby(key, "blocked", 1 if record.get("status") == "blocked" else 0)
                pipe.hincrby(key, "errors", 1 if record.get("status") not in ("ok", "blocked") else 0)
                pipe.expire(key, USAGE_RETENTION_SECONDS)
            pipe.zadd("usage:days", {day: record["ts"]})
            pipe.expire("usage:days", USAGE_RETENTION_SECONDS * 4)
            if record.get("user_id"):
                pipe.zadd(f"usage:day_users:{day}", {record["user_id"]: record["ts"]})
                pipe.expire(f"usage:day_users:{day}", USAGE_RETENTION_SECONDS)
                pipe.zadd("usage:users", {record["user_id"]: record["ts"]})
                pipe.expire("usage:users", USAGE_RETENTION_SECONDS)
            await pipe.execute()
    except Exception as err:
        logger.warning("Token 统计写入 Redis 失败（已写内存兜底）: %s", err)
        for key in keys:
            _memory_incr(key, record)


def to_int_counters(raw: dict) -> dict:
    out: dict = {}
    for key, value in (raw or {}).items():
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            out[key] = value
    return out


def derive_metrics(raw: dict) -> dict:
    """把累计计数补上平均值 / 错误率等派生指标"""
    data = to_int_counters(raw or {})
    data.setdefault("requests", 0)
    data.setdefault("prompt_tokens", 0)
    data.setdefault("completion_tokens", 0)
    data.setdefault("total_tokens", 0)
    data.setdefault("latency_ms", 0)
    data.setdefault("errors", 0)
    data.setdefault("blocked", 0)
    requests = data["requests"] or 0
    data["avg_tokens"] = round(data["total_tokens"] / requests, 1) if requests else 0
    data["avg_latency_ms"] = round(data["latency_ms"] / requests) if requests else 0
    # 错误率只算真正的失败；拦截率单独给（两者含义完全不同）
    data["error_rate"] = round(data["errors"] / requests * 100, 2) if requests else 0
    data["block_rate"] = round(data["blocked"] / requests * 100, 2) if requests else 0
    return data


async def _hgetall(prefix: str, client=None) -> dict[str, dict]:
    """读取某一类 key 的汇总（Redis 优先，失败回退内存）"""
    result: dict[str, dict] = {}
    try:
        client = client or redis_client.get_redis()
        for key in await client.keys(f"{prefix}:*"):
            # 去掉 "usage:route:" 这类前缀，只留业务标识（如 chat / deepseek-chat）
            result[key[len(prefix) + 1:]] = to_int_counters(await client.hgetall(key))
        return result
    except Exception as err:
        logger.warning("读取用量统计失败，回退内存: %s", err)
    marker = f"{prefix}:"
    for key, bucket in _memory_usage.items():
        if key.startswith(marker):
            result[key[len(marker):]] = dict(bucket)
    return result


async def purge_user_usage(user_id: str) -> dict:
    """清空某个用户的 Token 统计条目

    注意：只清「按用户」这一维（usage:user:{id} 与用户列表），
    总计 / 按天 / 按入口 / 按模型的历史累计无法按用户回滚。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return {"user_id": user_id, "cleared": False, "redis_ok": False}

    redis_ok = True
    try:
        client = redis_client.get_redis()
        pipe = client.pipeline(transaction=False)
        pipe.delete(f"usage:user:{user_id}")
        pipe.zrem("usage:users", user_id)
        await pipe.execute()
        for key in await client.keys("usage:day_users:*"):
            await client.zrem(key, user_id)
    except Exception as err:
        redis_ok = False
        logger.warning("清空用户用量统计失败: %s", err)

    _memory_usage.pop(f"usage:user:{user_id}", None)
    return {"user_id": user_id, "cleared": True, "redis_ok": redis_ok}


async def purge_all_usage() -> dict:
    """清空全部 Token 用量统计（总计 / 按天 / 按入口 / 按模型 / 按用户）"""
    deleted = 0
    redis_ok = True
    try:
        client = redis_client.get_redis()
        keys = await client.keys("usage:*")
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            if batch:
                deleted += int(await client.delete(*batch))
    except Exception as err:
        redis_ok = False
        logger.warning("清空全部用量统计失败: %s", err)

    _memory_usage.clear()
    return {"deleted_keys": deleted, "redis_ok": redis_ok}


async def get_usage_stats(day: Optional[str] = None) -> dict:
    """读取用量统计（保留原接口），day 缺省为今天（UTC）"""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result: dict = {"date": day, "total": {}, "today": {}, "routes": {}, "models": {}, "users": {}}
    try:
        client = redis_client.get_redis()
        result["total"] = derive_metrics(await client.hgetall("usage:total"))
        result["today"] = derive_metrics(await client.hgetall(f"usage:{day}"))
        result["routes"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:route", client)).items()}
        result["models"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:model", client)).items()}
        result["users"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:user", client)).items()}
        result["days"] = sorted(await client.zrange("usage:days", 0, -1), reverse=True)
    except Exception as err:
        logger.warning("读取用量统计失败，回退内存: %s", err)
        result["error"] = "Redis 不可用（数据来自内存兜底）"
        result["total"] = derive_metrics(_memory_usage.get("usage:total", {}))
        result["today"] = derive_metrics(_memory_usage.get(f"usage:{day}", {}))
        result["routes"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:route")).items()}
        result["models"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:model")).items()}
        result["users"] = {k: derive_metrics(v) for k, v in (await _hgetall("usage:user")).items()}
        result["days"] = chatlog.memory_days()
    return result


async def get_usage_overview(days: int = 7) -> dict:
    """管理员看板用：今日 / 累计 / 按入口 / 按模型 / Top 用户 / 近 N 天趋势"""
    days = max(1, min(int(days), 30))
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    stats = await get_usage_stats(today)
    routes = {k: derive_metrics(v) for k, v in stats["routes"].items()}
    models = {k: derive_metrics(v) for k, v in stats["models"].items()}
    users = {k: derive_metrics(v) for k, v in stats["users"].items()}

    trend: list[dict] = []
    try:
        client = redis_client.get_redis()
        async with client.pipeline(transaction=False) as pipe:
            for offset in range(days - 1, -1, -1):
                day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
                pipe.hgetall(f"usage:{day}")
                pipe.zcard(f"usage:day_users:{day}")
            values = await pipe.execute()
        for index in range(days):
            day = (now - timedelta(days=days - 1 - index)).strftime("%Y-%m-%d")
            trend.append({"date": day, **derive_metrics(values[index * 2] or {}),
                          "active_users": int(values[index * 2 + 1] or 0)})
    except Exception as err:
        logger.warning("读取趋势失败，回退内存: %s", err)
        for offset in range(days - 1, -1, -1):
            day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            trend.append({"date": day, **derive_metrics(_memory_usage.get(f"usage:{day}", {})), "active_users": 0})

    # 今日活跃用户 = 今日有调用记录的用户数（trend 最后一天即今天）
    if trend:
        stats["today"]["active_users"] = trend[-1]["active_users"]

    top_users = sorted(
        [{"user_id": uid, **data} for uid, data in users.items()],
        key=lambda item: item.get("total_tokens", 0),
        reverse=True,
    )[:USAGE_TOP_N]

    return {
        "date": today,
        "days": days,
        "today": stats["today"],
        "total": stats["total"],
        "routes": routes,
        "models": models,
        "top_users": top_users,
        "trend": trend,
        "redis_available": "error" not in stats,
    }
