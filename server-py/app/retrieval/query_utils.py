"""检索辅助：口语化查询归一 + 关键词兜底检索

这里只依赖标准库与 langchain_core，不碰向量库/embedding，
所以可以独立单测、也能被其他链路复用（RAG 页、中枢的知识库节点、工具）。
"""
import os
import re

from langchain_core.documents import Document

# 知识库原始文档目录（用于「查看原文」和关键词兜底）
KNOWLEDGE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")
)

# 口语化尾巴/语气词：会稀释向量相似度，embedding 前先去掉
_COLLOQUIAL_TAIL = re.compile(
    r"(咋卖|怎么卖|咋样|怎么样|咋办|多少钱啊?|价格呢|有没有卖|有没有|有吗|能买吗|想买|"
    r"一下|啊|呀|呢|吧|吗|么|呗|哦|嘛|哈|嘞|啦)+$"
)


# 纯寒暄：这类句子不该去检索知识库
# （"你好呀" 曾经命中过「联系客服」章节 —— 相似度 0.43 刚好过阈值，
#   结果一句问候后面跟了条"依据"，很怪）
_SMALL_TALK_RE = re.compile(
    r"^\s*(你好|您好|在吗|在么|嗨|哈喽|hi|hello|哈罗|早上好|下午好|晚上好|早|"
    r"谢谢|多谢|感谢|辛苦了|辛苦|再见|拜拜|回见|好的|好嘞|收到|嗯嗯|哦哦|"
    r"哈哈+|嘿嘿|呵呵|没问题|ok)"
    r"(\s|呀|啊|哦|呢|吧|哈|~|～|！|!|。|\.|，|,|、)*$",
    re.IGNORECASE,
)


def is_small_talk(text: str) -> bool:
    """整句就是寒暄时返回 True（那就别去查知识库了）"""
    return bool(_SMALL_TALK_RE.match(text or ""))


def build_source_line(sources, limit: int = 3) -> str:
    """给没有来源面板的页面用的「依据」行

    例：📎 依据：products.md#便携充电宝 20000mAh（相似度 0.59）、policies.md#退货政策（0.46）
    """
    items = [item for item in (sources or []) if item.get("source")][:limit]
    if not items:
        return ""
    parts = []
    for item in items:
        score = item.get("score")
        label = item["source"]
        if isinstance(score, (int, float)):
            label = "%s（相似度 %.2f）" % (label, float(score))
        parts.append(label)
    return "\n\n📎 依据：%s" % "、".join(parts)


def normalize_query(question: str) -> str:
    """把口语化问句收拾成更好检索的形式

    实测：'耳机咋卖' 的向量最高分只有 0.3858（被'咋卖'稀释），低于阈值 0.4 会一条都留不下；
    去掉口语尾巴变成 '耳机' 后能到 0.4508，正常召回。
    """
    text = re.sub(r"[?？!！。，,~～\s]+$", "", (question or "").strip())
    cleaned = _COLLOQUIAL_TAIL.sub("", text).strip()
    # 别把问句削没了（例如整句就是一个语气词）
    return cleaned if len(cleaned) >= 2 else text


# 停用词：中文子串匹配很容易被"怎么/什么/多少"这类词带偏，先剔除
_STOPWORDS = (
    "怎么", "怎样", "如何", "什么", "哪些", "哪个", "哪里", "为什么", "多少", "多久", "几天",
    "可以", "能否", "是否", "请问", "帮我", "给我", "我想", "我要", "你们", "我们", "他们",
    "一下", "这个", "那个", "这些", "那些", "的话", "然后", "就是", "还是", "或者",
    "python", "能不能", "有没有",
)


def _strip_stopwords(text: str) -> str:
    cleaned = text
    for word in _STOPWORDS:
        cleaned = cleaned.replace(word, "")
    return cleaned


_sections_cache: dict = {"mtimes": None, "sections": []}


def load_sections() -> list[dict]:
    """把知识库 markdown 按 ## 切成章节（带缓存，文件变了才重读）"""
    try:
        files = sorted(name for name in os.listdir(KNOWLEDGE_DIR) if name.endswith(".md"))
    except OSError:
        return []
    try:
        mtimes = tuple(os.path.getmtime(os.path.join(KNOWLEDGE_DIR, name)) for name in files)
    except OSError:
        mtimes = None
    if mtimes is not None and _sections_cache["mtimes"] == mtimes:
        return _sections_cache["sections"]

    sections: list[dict] = []
    for name in files:
        with open(os.path.join(KNOWLEDGE_DIR, name), "r", encoding="utf-8") as handle:
            content = handle.read()
        title, buffer = "概述", []
        for line in content.splitlines():
            if line.startswith("## "):
                sections.append({"file": name, "title": title, "content": "\n".join(buffer).strip()})
                title, buffer = line[3:].strip(), [line]
            else:
                buffer.append(line)
        sections.append({"file": name, "title": title, "content": "\n".join(buffer).strip()})

    sections = [item for item in sections if len(item["content"]) >= 10]
    _sections_cache.update({"mtimes": mtimes, "sections": sections})
    return sections


def keyword_search(question: str, limit: int = 3, min_score: float = 0.33) -> list:
    """关键词兜底：按查询片段与章节标题/正文做子串匹配捞回章节

    中文不需要分词，用长度 2~6 的连续子串去比就够用：
    '耳机咋卖' 能匹配到标题「蓝牙耳机 X1 Pro」「降噪头戴耳机 H7」。

    返回 [(Document, score)]，Document.metadata 里带 match="keyword" 以便区分来源。
    """
    query = _strip_stopwords(re.sub(r"\s+", "", question or ""))
    if len(query) < 2:
        return []

    scored: list[tuple[int, dict]] = []
    for section in load_sections():
        title = re.sub(r"\s+", "", section["title"])
        body = re.sub(r"\s+", "", section["content"])
        best = 0
        for size in range(min(6, len(query)), 1, -1):
            for start in range(0, len(query) - size + 1):
                fragment = query[start:start + size]
                if fragment in title:
                    best = max(best, size * 3)
                elif fragment in body:
                    best = max(best, size)
            if best >= size * 3:      # 标题命中就够了，不再往下找
                break
        if best >= 2:                 # 至少要有 2 个字的命中，避免"今天天气"这类乱匹配
            scored.append((best, section))

    scored.sort(key=lambda item: item[0], reverse=True)

    hits = []
    for rank, (_score, section) in enumerate(scored[:limit]):
        doc = Document(
            page_content=section["content"],
            metadata={
                "source": f"{section['file']}#{section['title']}",
                "section": section["title"],
                "match": "keyword",
            },
        )
        hits.append((doc, round(max(min_score, 0.32) + 0.01 - rank * 0.005, 4)))
    return hits


def rrf_fuse(*ranked_lists, limit: int = 4, k: int = 60) -> list:
    """Reciprocal Rank Fusion：把多路检索结果按排名融合

    score = Σ 1/(k + rank)，两路都排前面的结果胜出。
    这里只用来**排序**，对外仍然保留原始分数（向量相似度 / 关键词分），
    方便前端和后台照常展示"相似度 0.4508"，同时把 rrf 分数写进 metadata 供链路追踪。

    为什么用 RRF 而不是分数加权：向量相似度和关键词命中分不在一个量纲上，
    直接加权需要调参且不稳；RRF 只看排名，天然免调参（k=60 是论文常用值）。
    """
    scores: dict[str, float] = {}
    docs: dict[str, object] = {}
    best_score: dict[str, float] = {}

    for ranked in ranked_lists:
        for rank, (doc, score) in enumerate(ranked or [], start=1):
            source = (doc.metadata or {}).get("source", "")
            scores[source] = scores.get(source, 0.0) + 1.0 / (k + rank)
            best_score[source] = max(best_score.get(source, 0.0), float(score or 0.0))
            docs.setdefault(source, doc)

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
    fused = []
    for source, rrf_score in ordered:
        doc = docs[source]
        doc.metadata = {**(doc.metadata or {}), "rrf_score": round(rrf_score, 6)}
        fused.append((doc, best_score.get(source, 0.0)))
    return fused


def strip_citations(text: str) -> str:
    """去掉回答里的 [1] 编号（没有来源列表展示的场景，如多 Agent 中枢）"""
    return re.sub(r"\s*\[\d{1,2}\]", "", text or "").strip()
