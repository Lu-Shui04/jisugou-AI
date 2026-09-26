"""LangGraph 状态定义"""
from typing import Annotated, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class GraphState(TypedDict):
    messages: Annotated[list, add_messages]

    user_input: str

    # 主意图（兼容旧字段）：order | knowledge | general
    intent: str
    # 一句话可能同时涉及多个意图，例如 ["order", "knowledge"]
    intents: list[str]

    order_result: Optional[dict]
    rag_result: str
    # 知识库命中的片段（智能中枢用来展示"依据来源"）
    rag_sources: list[dict]
    final_answer: str
