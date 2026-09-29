"""给没有知识库来源面板的页面借用同一套知识库检索

适用入口：基础对话 / 订单查询 / 智能中枢。
这些页面自己没有检索能力（或者没有"引用来源"面板），以前只能让大模型自由发挥 ——
容易编出知识库里根本没写过的价格、天数。

这里复用知识库页面的检索链路，并额外做一件事：**把来源标出来**。
    有出处的回答才是可信的回答 —— 用户看到"依据：products.md#便携充电宝 20000mAh"
    才知道这个价格是查出来的，不是编的。

任何异常都降级成兜底话术，绝不把异常抛出去堵住链路（fail-open）。
"""
import asyncio
from typing import AsyncIterator, Optional

from app.retrieval.query_utils import build_source_line, is_small_talk  # noqa: F401  （对外复用）
from app.retrieval.query_utils import strip_citations
from app.retrieval.rag_chain import rag_chain_with_sources, stream_answer

FALLBACK = (
    "亲，这条问题知识库里暂时没查到，可以换个说法再问一次，"
    "或者联系人工客服 400-888-8888 帮您处理～"
)


async def prepare(question: str, config=None, **extra) -> dict:
    """检索 + 准备回答；失败返回 {}（调用方据此回退到原链路）"""
    # 纯寒暄不检索：'你好呀' 曾经命中过「联系客服」章节（0.43 刚好过阈值），
    # 结果一句问候后面挂了一条"依据"，很怪
    if is_small_talk(question):
        return {}

    payload = {"question": question, **extra}
    try:
        return await asyncio.to_thread(
            rag_chain_with_sources.prepare, payload, config=config
        )
    except Exception as err:                      # 检索链路挂了也不能堵死
        print("[KB bridge] 检索失败，降级兜底：%s" % err)
        return {}


async def stream_prepared(prepared: dict, question: str, config=None) -> AsyncIterator[str]:
    """基于已检索好的片段产出回答（没有命中时用链路自带的兜底话术）"""
    if not prepared:
        yield FALLBACK
        return

    # 没命中阈值 / 检索降级：链路自己带了兜底话术，直接用
    if prepared.get("answer"):
        yield prepared["answer"]
        return

    # 没有来源面板的页面：先攒齐再吐，顺便把 [1][2] 编号去掉
    buffer = ""
    try:
        async for chunk in stream_answer(prepared["docs"], question, config=config):
            buffer += chunk
    except Exception as err:
        print("[KB bridge] 生成失败，降级兜底：%s" % err)
        yield FALLBACK
        return
    yield strip_citations(buffer)


async def stream(question: str, config=None) -> AsyncIterator[str]:
    """检索 + 生成一步到位（调用方不关心来源时用）"""
    prepared = await prepare(question, config=config)
    async for chunk in stream_prepared(prepared, question, config=config):
        yield chunk


def source_line(sources: Optional[list], limit: int = 3) -> str:
    """「依据」行（实现放在 query_utils：那边不依赖向量库，方便单测）"""
    return build_source_line(sources, limit)


def sources_of(prepared: dict) -> list:
    return list((prepared or {}).get("sources") or [])
