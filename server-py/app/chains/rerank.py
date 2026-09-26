"""可选重排（rerank）：智谱 /rerank 接口

要不要用重排，取决于知识库规模：

- **现阶段（20 个片段 + Top-K=4）**：向量召回本身就准（15/15 命中正确来源），
  重排只会平白多一次 HTTP 调用，所以默认**关闭**；
- **知识库涨到几百/几千片段时**：先粗排召回 top-20~50，再用重排精排保留 top-4~6，
  这时收益明显（粗排的 top1 经常不是最相关的）。

配置（.env）：

    RAG_RERANK_ENABLED=false        # 总开关，默认关
    RAG_RERANK_MODEL=rerank         # 智谱重排模型
    RAG_RERANK_TOP_N=4              # 重排后保留前 N 条
    RAG_RERANK_MIN_CANDIDATES=2     # 候选太少就不值得调接口
    RAG_RERANK_TIMEOUT_SECONDS=3

Key 复用 SECURITY_ZHIPU_API_KEY / ZHIPU_API_KEY。
接口失败一律 fail-open：原样返回候选，不影响主流程。
"""
import logging
import os
import time

logger = logging.getLogger("jisu.rerank")

RERANK_ENABLED = os.getenv("RAG_RERANK_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
RERANK_MODEL = os.getenv("RAG_RERANK_MODEL", "rerank")
RERANK_TOP_N = int(os.getenv("RAG_RERANK_TOP_N", "4"))
RERANK_MIN_CANDIDATES = int(os.getenv("RAG_RERANK_MIN_CANDIDATES", "2"))
RERANK_TIMEOUT = float(os.getenv("RAG_RERANK_TIMEOUT_SECONDS", "3"))
RERANK_BASE_URL = os.getenv("SECURITY_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")


def api_key() -> str:
    return (os.getenv("SECURITY_ZHIPU_API_KEY") or os.getenv("ZHIPU_API_KEY") or "").strip()


def enabled() -> bool:
    return RERANK_ENABLED and bool(api_key())


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
    }
    headers = {"Authorization": "Bearer " + api_key(), "Content-Type": "application/json"}

    started = time.perf_counter()
    try:
        import httpx

        with httpx.Client(timeout=RERANK_TIMEOUT) as client:
            response = client.post(RERANK_BASE_URL.rstrip("/") + "/rerank",
                                   json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    except Exception as err:
        logger.warning("重排调用失败，保持原顺序：%s", err)
        return hits, {"enabled": True, "error": str(err)[:200], "before": before, "after": before}

    latency_ms = int((time.perf_counter() - started) * 1000)
    tokens = int((data.get("usage") or {}).get("total_tokens") or 0)

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
        # 注意：智谱 rerank 的分数几乎都在 0.999+（实测 4 位小数下全是 1.0000），
        # 只能当**排序**依据，不能当阈值依据；所以对外仍保留原来的相似度分，
        # 重排分只写进 metadata 供后台对照"重排前后的名次变化"。
        reordered.append((doc, original_score))

    if not reordered:
        return hits, {"enabled": True, "error": "重排返回为空", "before": before, "after": before}

    after = [(doc.metadata or {}).get("source", "") for doc, _ in reordered]
    changed = sum(1 for i, src in enumerate(after) if i < len(before) and before[i] != src)
    return reordered, {
        "enabled": True,
        "model": RERANK_MODEL,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "changed": changed,
        "before": before,
        "after": after,
    }
