"""RAG 检索链：Top-K 召回 + 相似度阈值过滤 + 检索增强生成

相似度阈值过滤：
    先按 Top-K（默认 4）召回，再逐个算相似度，低于 RAG_SCORE_THRESHOLD
    （默认 0.5）的低相关片段直接丢弃，不进入 Prompt。
    阈值过滤后没有任何片段时，不调用大模型，直接返回兜底话术，避免模型
    对着无关内容编答案。
"""
import logging
import os

from langchain_core.output_parsers import StrOutputParser
from langchain_postgres import PGVector

from app.db.postgres import PG_CONNECTION_STRING
from app.models.deepseek import create_model
from app.models.embedding import embeddings
from app.prompts.rag import condense_prompt, rag_prompt
from app.resilience import CircuitOpenError
from app.retrieval import rerank as rerank_module
from app.retrieval.query_utils import keyword_search, normalize_query, rrf_fuse
from app.observability.usage import record_retrieval
from app.security import rules as security_rules
from app.security import store as security_store

logger = logging.getLogger("jisu.rag")

COLLECTION_NAME = "knowledge_embeddings"

# 召回数量与相似度阈值（可在 .env 覆盖，也可在请求里按次覆盖）
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "4"))
RAG_SCORE_THRESHOLD = float(os.getenv("RAG_SCORE_THRESHOLD", "0.4"))

# 阈值过滤后没有命中任何片段时的兜底回答
NO_CONTEXT_ANSWER = (
    "亲，抱歉呀，知识库里暂时没有查到相关信息。"
    "您可以换个说法再问一次，或者联系人工客服 400-888-8888 帮您处理～"
)

# 向量库/模型不可用时的降级回答（不把异常抛给用户）
SERVICE_BUSY_ANSWER = (
    "亲，知识库这会儿有点忙，请稍后再试一次，"
    "或者联系人工客服 400-888-8888 帮您处理～"
)

vector_store = PGVector(
    embeddings=embeddings,
    collection_name=COLLECTION_NAME,
    connection=PG_CONNECTION_STRING,
    use_jsonb=True,
)


def doc_label(doc) -> str:
    """片段的可读来源标签，如 products.md#蓝牙耳机 X1 Pro"""
    meta = doc.metadata or {}
    return meta.get("source") or meta.get("section") or "知识库"


def format_docs(docs):
    """命中片段编号后拼成上下文，模型据此用 [1][2] 引用"""
    blocks = [
        f"[{index}] 来源：{doc_label(doc)}\n{doc.page_content}"
        for index, doc in enumerate(docs, start=1)
    ]
    return "\n\n---\n\n".join(blocks)


def build_source(doc, score: float, index: int) -> dict:
    """给前端的来源对象：编号 / 文件 / 章节 / 相似度 / 完整片段

    content 传完整片段（不再截断），前端「查看片段」「查看原文」可以逐字核对。
    """
    meta = doc.metadata or {}
    source = doc_label(doc)
    file_name, _, tail = source.partition("#")
    return {
        "index": index,
        "source": source,
        "file": file_name,
        "section": meta.get("section") or tail,
        "part": meta.get("part"),
        "score": round(float(score), 4),
        # vector=向量命中 / keyword=向量没召回时的关键词兜底
        "match": meta.get("match") or "vector",
        "preview": doc.page_content[:120].replace("\n", " "),
        "content": doc.page_content,
    }


def _to_similarity(distance) -> float:
    """PGVector 默认返回余弦距离，转换成 [0, 1] 的相似度"""
    try:
        return max(0.0, min(1.0, 1.0 - float(distance)))
    except (TypeError, ValueError):
        return 0.0


def retrieve_candidates(question, top_k=None):
    """Top-K 召回的全部候选片段（还没做阈值过滤），带相似度分数

    向量化前先做口语化归一：'耳机咋卖' → '耳机'，否则相似度会被语气词稀释到阈值以下。
    """
    k = RAG_TOP_K if top_k is None else int(top_k)
    query = normalize_query(question)
    pairs = vector_store.similarity_search_with_score(query, k=k)
    return [(doc, _to_similarity(distance)) for doc, distance in pairs]


def apply_threshold(candidates, threshold):
    """重排（过滤）：相似度低于阈值的低相关片段直接丢弃"""
    return [(doc, score) for doc, score in candidates if score >= threshold]


def retrieve_with_threshold(question, top_k=None, score_threshold=None):
    """向量召回 + 阈值过滤 + 关键词召回，两路用 RRF 融合，返回 [(Document, score)]

    - 阈值只作用在**向量**这一路（决定"向量有没有足够相关的候选"）
    - 关键词这一路是零成本的本地子串匹配，命中就参与融合
    - 融合只影响排序，对外仍然返回原始分数（向量相似度 / 关键词分），展示口径不变
    """
    k = RAG_TOP_K if top_k is None else int(top_k)
    threshold = RAG_SCORE_THRESHOLD if score_threshold is None else float(score_threshold)
    try:
        vector_kept = apply_threshold(retrieve_candidates(question, k), threshold)
    except CircuitOpenError as err:
        # 同上：Embedding 熔断时不抛错，只保留关键词这一路
        logger.warning("Embedding 熔断（%s），retrieve_with_threshold 改用关键词召回", err.name)
        vector_kept = []
    keyword_hits = keyword_search(question, limit=k)
    return rrf_fuse(vector_kept, keyword_hits, limit=k)


def _record_detail(config, question, top_k, threshold, candidates, kept,
                   degraded=False, error="", rerank_stats=None):
    """把「检索 + 重排」明细记进本次请求的采集器（管理员后台可查）"""
    kept_ids = {id(doc) for doc, _ in kept}
    record_retrieval(
        config,
        query=question,
        top_k=top_k,
        threshold=round(threshold, 4),
        embedded_query=normalize_query(question),
        kept=len(kept),
        filtered=not kept,
        degraded=degraded,
        error=error,
        # 重排策略按实际生效的来写（开启时是硅基流动 bge-reranker-v2-m3，关闭时是阈值过滤）
        rerank=rerank_module.describe(),
        rerank_stats=rerank_stats or {},
        candidates=[
            {
                "source": (doc.metadata or {}).get("source") or "未知来源",
                "score": score,
                "content": doc.page_content,
                "kept": id(doc) in kept_ids,
                "match": (doc.metadata or {}).get("match") or "vector",
                "rank_after": (doc.metadata or {}).get("rank_after"),
                "rerank_score": (doc.metadata or {}).get("rerank_score"),
            }
            for doc, score in candidates
        ],
    )


def _retrieve_with_detail(question, top_k, score_threshold, config):
    """检索 + 重排，并把明细挂到采集器；返回 (hits, threshold)

    检索失败会记一条 degraded 明细后抛出，由调用方降级成可读话术。
    """
    k = RAG_TOP_K if top_k is None else int(top_k)
    threshold = RAG_SCORE_THRESHOLD if score_threshold is None else float(score_threshold)
    circuit_note = ""
    try:
        candidates = retrieve_candidates(question, k)
    except CircuitOpenError as err:
        # Embedding 熔断：跳过向量召回，只走关键词这一路（本地子串匹配），
        # 知识库仍然答得出来，比"知识库这会儿有点忙"体验好得多
        candidates = []
        circuit_note = "Embedding 熔断（%s），已跳过向量召回、改用关键词召回" % err.name
        logger.warning("%s：%s", circuit_note, question[:40])
    except Exception as err:
        _record_detail(config, question, k, threshold, [], [], degraded=True, error=str(err))
        raise
    # ① 向量召回（阈值过滤）+ ② 关键词召回 → ③ RRF 融合
    vector_kept = apply_threshold(candidates, threshold)
    keyword_hits = keyword_search(question, limit=k)
    hits = rrf_fuse(vector_kept, keyword_hits, limit=k)
    if keyword_hits:
        logger.info("关键词召回 %d 条，RRF 融合后保留 %d 条：%s", len(keyword_hits), len(hits),
                    [doc.metadata.get("source") for doc, _ in hits])

    # ④ 可选重排（默认关闭；知识库变大或候选变多时开启收益明显）
    rerank_stats = {}
    if rerank_module.enabled() and len(hits) >= 2:
        hits, rerank_stats = rerank_module.rerank(question, hits)
        if rerank_stats.get("enabled"):
            logger.info("重排 %s：%d 条候选，%d 条名次变化，耗时 %sms",
                        rerank_stats.get("model"), len(hits), rerank_stats.get("changed"),
                        rerank_stats.get("latency_ms"))

    _record_detail(config, question, k, threshold, candidates, hits, degraded=bool(circuit_note),
                   error=circuit_note, rerank_stats=rerank_stats)
    return hits, threshold


_model = create_model(temperature=0)

# ── 查询改写：用户说"这是什么商品"这种依赖上文的话时，先补成完整问题再检索 ──
_FOLLOWUP_WORDS = ("这个", "那个", "它", "该", "此", "上面", "刚才", "这些", "那些", "哪个", "哪一种")

_condense_chain = condense_prompt | create_model(temperature=0) | StrOutputParser()


def _needs_condense(question: str, history: str) -> bool:
    """问题很短或含指代词，说明它依赖上文，需要改写"""
    if not (history or "").strip():
        return False
    text = (question or "").strip()
    return len(text) <= 12 or any(word in text for word in _FOLLOWUP_WORDS)


def condense_question(question: str, history: str, config=None) -> str:
    """结合上文把问题补完整；失败就退回原问题，不影响主流程"""
    if not _needs_condense(question, history):
        return question
    try:
        rewritten = _condense_chain.invoke(
            {"question": question, "history": history}, config=config
        ).strip().strip('"“” ')
        if rewritten:
            logger.info("查询改写：%s → %s", question, rewritten)
            return rewritten
    except Exception as err:
        logger.warning("查询改写失败，用原问题检索：%s", err)
    return question


def safe_context(docs) -> str:
    """拼好检索上下文，并剔除片段里夹带的指令（防知识库间接注入）"""
    text = format_docs(docs)
    cleaned, hits = security_rules.sanitize_context(text)
    if hits:
        security_store.bump_sync("context_sanitized")
        logger.warning("知识库上下文清洗：剔除 %d 处可疑指令 %s", len(hits), sorted(set(hits)))
    return cleaned


def _build_answer_chain(model=None):
    return (
        (lambda input: {
            "context": safe_context(input["docs"]),
            "question": input["question"],
            "chat_history": input.get("chat_history") or [],
        })
        | rag_prompt
        | (model or _model)
        | StrOutputParser()
    )


_answer_chain = _build_answer_chain()

# 流式回答链：知识库问答页要逐 token 出字，模型必须开 streaming
_streaming_answer_chain = _build_answer_chain(create_model(temperature=0, streaming=True))


async def stream_answer(docs, question, config=None, chat_history=None):
    """逐 token 生成回答（异步生成器）"""
    async for chunk in _streaming_answer_chain.astream(
        {"docs": docs, "question": question, "chat_history": chat_history or []},
        config=config,
    ):
        if chunk:
            yield chunk


class RagChain:
    """带来源引用 + 相似度分数的 RAG 链"""

    def prepare(self, input: dict, config=None) -> dict:
        """只做「问题改写 + 检索 + 降级判断」，把生成留给调用方

        流式路由先用它拿到 sources（可以立刻推给前端），再逐 token 生成回答。

        返回：
          sources —— 命中的来源（编号 / 文件 / 章节 / 完整片段）
          docs    —— 命中的 Document，用于生成回答
          answer  —— 兜底话术（检索失败或没命中时直接回给用户）；正常情况为空串
        """
        threshold = RAG_SCORE_THRESHOLD if input.get("score_threshold") is None else input["score_threshold"]

        # 多轮：先结合上文把问题改写完整再检索
        query = condense_question(input["question"], input.get("history") or "", config)

        try:
            hits, _ = _retrieve_with_detail(
                query, input.get("top_k"), input.get("score_threshold"), config
            )
        except Exception as err:
            logger.error("知识库检索失败，降级回答：%s", err)
            return {"sources": [], "docs": [], "answer": SERVICE_BUSY_ANSWER,
                    "filtered": True, "degraded": True, "threshold": threshold, "query": query}

        if not hits:
            return {"sources": [], "docs": [], "answer": NO_CONTEXT_ANSWER,
                    "filtered": True, "degraded": False, "threshold": threshold, "query": query}

        return {
            "sources": [
                build_source(doc, score, index)
                for index, (doc, score) in enumerate(hits, start=1)
            ],
            "docs": [doc for doc, _ in hits],
            "answer": "",
            "filtered": False,
            "degraded": False,
            "threshold": threshold,
            "query": query,
        }

    def invoke(self, input: dict, config=None) -> dict:
        prepared = self.prepare(input, config)

        if prepared["answer"]:
            return {"answer": prepared["answer"], "sources": prepared["sources"],
                    "filtered": prepared["filtered"], "degraded": prepared["degraded"],
                    "threshold": prepared["threshold"], "query": prepared["query"]}

        try:
            answer = _answer_chain.invoke(
                {
                    "docs": prepared["docs"],
                    "question": input["question"],
                    "chat_history": input.get("chat_history"),
                },
                config=config,
            )
        except Exception as err:
            logger.error("RAG 回答生成失败，降级回答：%s", err)
            return {"answer": SERVICE_BUSY_ANSWER, "sources": [], "filtered": True,
                    "degraded": True, "threshold": prepared["threshold"]}
        return {"answer": answer, "sources": prepared["sources"], "filtered": False,
                "degraded": False, "query": prepared["query"]}


rag_chain_with_sources = RagChain()