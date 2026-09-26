"""可选重排（rerank）：硅基流动（SiliconFlow）/rerank 接口

模型：BAAI/bge-reranker-v2-m3（Cross-Encoder 精排，中文场景稳）

为什么换成硅基流动的 bge-reranker-v2-m3：
- 智谱 rerank 的分数接近饱和（实测 4 位小数下全是 1.0000），只能当排序用、不能当阈值用；
- bge-reranker-v2-m3 的分数是**可区分的**（实测同一问句下 0.8263 / 0.0837 / 0.0008），
  既能排序、也能当相关性参考，后台"重排前后名次变化"看起来才不是一排 1.0000。

配置（.env）：

    RAG_RERANK_ENABLED=true                      # 总开关
    RAG_RERANK_BASE_URL=https://api.siliconflow.cn/v1
    RAG_RERANK_API_KEY=sk-xxxx                   # 留空则回退 SILICONFLOW_API_KEY
    RAG_RERANK_MODEL=BAAI/bge-reranker-v2-m3     # 硅基流动重排模型
    RAG_RERANK_TOP_N=4                           # 重排后保留前 N 条
    RAG_RERANK_MIN_CANDIDATES=2                  # 候选太少就不值得调接口
    RAG_RERANK_TIMEOUT_SECONDS=3

四层容错（和 embedding / 安全小模型那套一致）：
    ① 超时：总超时 3s、连接超时 1.5s（连连接都建不起来就别耗着）；
    ② 熔断：rerank:siliconflow —— 连续失败 / 失败率超标就跳闸，之后直接跳过重排（0ms，不发请求）；
    ③ 省调用：候选少于 RAG_RERANK_MIN_CANDIDATES 根本不发请求；
    ④ 降级：任何失败（含熔断打开）都 fail-open —— 原样返回候选，问答主流程完全不受影响。

后台「系统状态」页能看到 rerank:siliconflow 的熔断状态；检索明细里会标出这次是
正常重排、调用失败保持原顺序，还是熔断跳过。
"""
import logging
import os
import time

from app.resilience import CircuitOpenError, get_breaker

logger = logging.getLogger("jisu.rerank")

PROVIDER = "硅基流动 SiliconFlow"
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"

RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
RERANK_MODEL = (os.getenv("RAG_RERANK_MODEL") or DEFAULT_MODEL).strip()
RERANK_TOP_N = int(os.getenv("RAG_RERANK_TOP_N", "4"))
RERANK_MIN_CANDIDATES = int(os.getenv("RAG_RERANK_MIN_CANDIDATES", "2"))
RERANK_TIMEOUT = float(os.getenv("RAG_RERANK_TIMEOUT_SECONDS", "3"))
RERANK_CONNECT_TIMEOUT = float(os.getenv("RAG_RERANK_CONNECT_TIMEOUT_SECONDS", "1.5"))
RERANK_BASE_URL = (os.getenv("RAG_RERANK_BASE_URL") or DEFAULT_BASE_URL).strip()
# 熔断粒度：重排是"锦上添花"的一步，下游挂了就该立刻跳过，绝不能拖累问答
RERANK_BREAKER = "rerank:siliconflow"


def api_key() -> str:
    """重排专用 Key，没配就回退到通用的 SILICONFLOW_API_KEY"""
    return (os.getenv("RAG_RERANK_API_KEY") or os.getenv("SILICONFLOW_API_KEY") or "").strip()


def enabled() -> bool:
    return RERANK_ENABLED and bool(api_key())


def describe() -> str:
    """给后台 /admin 系统页展示的重排策略文案（一眼看出用的是谁家的哪个模型）"""
    if not RERANK_ENABLED:
        return "相似度阈值过滤（score >= threshold 保留）"
    if not api_key():
        return "重排已开启但缺少 RAG_RERANK_API_KEY，实际仍走阈值过滤"
    return "%s 重排（%s，Top-N=%d）" % (PROVIDER, RERANK_MODEL, RERANK_TOP_N)


def _total_tokens(data: dict) -> int:
    """Token 数兼容两种返回：OpenAI 风格 usage.total_tokens / 硅基流动 meta.tokens.input_tokens"""
    total = (data.get("usage") or {}).get("total_tokens")
    if total:
        return int(total)
    meta_tokens = (data.get("meta") or {}).get("tokens") or {}
    return int(meta_tokens.get("input_tokens") or meta_tokens.get("total_tokens") or 0)


def _post_rerank(payload: dict, headers: dict) -> dict:
    """真正发请求：连接/读超时分开设 + 熔断保护；失败与熔断交给调用方统一降级"""
    import httpx

    timeout = httpx.Timeout(RERANK_TIMEOUT, connect=min(RERANK_CONNECT_TIMEOUT, RERANK_TIMEOUT))
    with get_breaker(RERANK_BREAKER).guard():
        with httpx.Client(timeout=timeout) as client:
            response = client.post(RERANK_BASE_URL.rstrip("/") + "/rerank",
                                   json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def rerank(question: str, hits: list, top_n: int = None) -> tuple[list, dict]:
    """对候选做重排，返回 (重排后的 hits, 统计信息)

    hits: [(Document, score)]
    统计信息里有 before/after 的顺序，方便后台展示"重排前后名次变化"。
    """
    top_n = top_n or RERANK_TOP_N
    before = [(doc.metadata or {}).get("source", "") for doc, _ in hits]

    if not enabled() or len(hits) < RERANK_MIN_CANDIDATES:
        return hits, {"enabled": False, "reason": "未开启或候选过少"}

    documents = [doc.page_content for doc, _ in hits]
    payload = {
        "model": RERANK_MODEL,
        "query": question,
        "documents": documents,
        "top_n": min(top_n, len(documents)),
        # 不回传原文：候选正文本地就有，少传一遍省带宽
        "return_documents": False,
    }
    headers = {"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"}

    started = time.perf_counter()
    try:
        data = _post_rerank(payload, headers)
    except CircuitOpenError as err:
        # 熔断打开：连请求都不发，直接当"这次没重排"（降级里最快的一档）
        logger.warning("重排熔断打开，本次跳过重排：%s", err)
        return hits, {"enabled": True, "circuit": "open", "breaker": RERANK_BREAKER,
                      "error": str(err)[:200], "before": before, "after": before}
    except Exception as err:
        logger.warning("重排调用失败，保持原顺序：%s", err)
        return hits, {"enabled": True, "circuit": "closed", "breaker": RERANK_BREAKER,
                      "error": str(err)[:200], "before": before, "after": before}

    latency_ms = int((time.perf_counter() - started) * 1000)
    tokens = _total_tokens(data)

    reordered = []
    for rank_after, item in enumerate(data.get("results") or [], start=1):
        index = int(item.get("index", -1))
        if index < 0 or index >= len(hits):
            continue
        doc, original_score = hits[index]
        doc.metadata = {
            **(doc.metadata or {}),
            "rerank_score": round(float(item.get("relevance_score") or 0.0), 6),
            "rank_before": index + 1,
            "rank_after": rank_after,
        }
        # 硅基流动 bge-reranker-v2-m3 的分数量级和向量相似度不同（0.8 / 0.08 这种），
        # 而下游的 RAG_SCORE_THRESHOLD 是按向量相似度调的，所以对外仍保留原来的相似度分，
        # 重排分只写进 metadata，供后台对照"重排前后的名次变化"。
        reordered.append((doc, original_score))

    if not reordered:
        return hits, {"enabled": True, "error": "重排返回为空", "before": before, "after": before}

    after = [(doc.metadata or {}).get("source", "") for doc, _ in reordered]
    changed = sum(1 for i, src in enumerate(after) if i < len(before) and before[i] != src)
    return reordered, {
        "enabled": True,
        "circuit": "closed",
        "breaker": RERANK_BREAKER,
        "provider": PROVIDER,
        "model": RERANK_MODEL,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "changed": changed,
        "before": before,
        "after": after,
    }
