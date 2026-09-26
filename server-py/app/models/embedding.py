"""
Embedding 模型封装
方式一（推荐）：智谱 AI — 注册地址 https://open.bigmodel.cn
方式二：阿里云百炼 — 注册地址 https://bailian.console.aliyun.com
两种方式只有 model / api_key / base_url 三个字段不同，其余代码一样

超时 / 重试：向量化是外部服务，设了超时和重试，避免单次抖动把 RAG 链路拖死。
熔断：如果这个服务整体挂掉（连续失败 / 失败率超标），就不要再让每个请求都等
15s × 3 次重试了 —— 直接快速失败，由检索链路改用关键词召回（本地匹配，零成本）。
"""
import os

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.resilience import get_breaker

EMBEDDING_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "15"))
EMBEDDING_MAX_RETRIES = int(os.getenv("EMBEDDING_MAX_RETRIES", "2"))
EMBEDDING_BREAKER = "embedding:zhipu"

# 方式一：智谱 AI（默认）
_raw_embeddings = OpenAIEmbeddings(
    model="embedding-3",
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url="https://open.bigmodel.cn/api/paas/v4",
    check_embedding_ctx_length=False,
    timeout=EMBEDDING_TIMEOUT_SECONDS,
    max_retries=EMBEDDING_MAX_RETRIES,
)

# 方式二：阿里云百炼（注释掉方式一，取消注释此段）
# _raw_embeddings = OpenAIEmbeddings(
#     model="text-embedding-v3",
#     api_key=os.getenv("DASHSCOPE_API_KEY"),
#     base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
#     check_embedding_ctx_length=False,
#     timeout=EMBEDDING_TIMEOUT_SECONDS,
#     max_retries=EMBEDDING_MAX_RETRIES,
# )


class GuardedEmbeddings(Embeddings):
    """给任意 Embeddings 套一层熔断（对外接口完全不变，PGVector 无感）"""

    def __init__(self, inner: Embeddings, name: str = EMBEDDING_BREAKER):
        self._inner = inner
        self._breaker = get_breaker(name)

    def embed_documents(self, texts):
        with self._breaker.guard():
            return self._inner.embed_documents(texts)

    def embed_query(self, text):
        with self._breaker.guard():
            return self._inner.embed_query(text)

    def __getattr__(self, item):
        # 其余属性（model、dimensions 等）原样透传，避免上游拿到代理后取不到东西
        return getattr(self._inner, item)


embeddings = GuardedEmbeddings(_raw_embeddings)
