"""
文档入库脚本，执行一次即可，知识库更新时重新执行
运行：python -m app.scripts.ingest

切分策略：按 markdown 二级标题（## 商品名 / ## 政策名）切成独立片段，
一个商品一块，保证检索时能精确命中，不会被相邻商品稀释。
超过 chunk_size 的长章节再按 RecursiveCharacterTextSplitter 二次切分。
"""
import os

from langchain_core.documents import Document
from langchain_postgres import PGVector
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.db.postgres import PG_CONNECTION_STRING
from app.models.embedding import embeddings

COLLECTION_NAME = "knowledge_embeddings"

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

_KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "knowledge")


def _split_sections(content: str, source: str, splitter: RecursiveCharacterTextSplitter):
    """按 ## 标题切分：一个商品 / 一条政策 = 一个片段"""
    sections = []
    title = "概述"
    buffer = []

    for line in content.splitlines():
        if line.startswith("## "):
            sections.append((title, "\n".join(buffer).strip()))
            title = line[3:].strip()
            buffer = [line]
        else:
            buffer.append(line)
    sections.append((title, "\n".join(buffer).strip()))

    docs = []
    for title, text in sections:
        text = text.strip()
        if not text or len(text) < 10:
            continue
        meta = {"source": f"{source}#{title}", "section": title}
        if len(text) <= CHUNK_SIZE:
            docs.append(Document(page_content=text, metadata=meta))
        else:
            for index, piece in enumerate(splitter.split_text(text)):
                docs.append(Document(page_content=piece, metadata={**meta, "part": index}))
    return docs


def _load_docs():
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )

    docs = []
    for file in ["products.md", "policies.md"]:
        with open(os.path.join(_KNOWLEDGE_DIR, file), "r", encoding="utf-8") as f:
            content = f.read()
        docs.extend(_split_sections(content, file, splitter))
    return docs


def ingest():
    print("开始处理文档...")

    chunks = _load_docs()
    print(f"切分完成，共 {len(chunks)} 个片段")
    for chunk in chunks:
        print(f"  - {chunk.metadata['source']}（{len(chunk.page_content)} 字）")

    # 清空旧数据（全量更新场景）
    vector_store = PGVector(
        embeddings=embeddings,
        collection_name=COLLECTION_NAME,
        connection=PG_CONNECTION_STRING,
        use_jsonb=True,
        pre_delete_collection=True,
    )
    vector_store.add_documents(chunks)

    print("入库完成")


if __name__ == "__main__":
    ingest()
