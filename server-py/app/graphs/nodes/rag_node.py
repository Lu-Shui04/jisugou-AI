from langchain_core.runnables import RunnableConfig

from app.retrieval.rag_chain import rag_chain_with_sources, strip_citations


def _history_text(state, limit: int = 6) -> str:
    """最近几轮对话，用来把"这是什么商品"这类依赖上文的问题改写完整"""
    messages = state.get("messages") or []
    lines = []
    for message in messages[-limit:]:
        role = "用户" if getattr(message, "type", "") == "human" else "客服"
        content = (getattr(message, "content", "") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}：{content[:150]}")
    return "\n".join(lines)


def rag_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]
    chat_history = [
        ("human" if message.type == "human" else "assistant", message.content)
        for message in (state.get("messages") or [])[-6:]
    ]

    try:
        # 带上文：先改写问题，再做阈值过滤检索 + 生成。
        # 用「带来源」的链路（而不是纯文本链路），这样命中片段能一起交给前端展示"依据"——
        # 回答了知识库里的内容却不说来源，用户没法判断这个价格是不是编的。
        result = rag_chain_with_sources.invoke(
            {
                "question": user_input,
                "history": _history_text(state),
                "chat_history": chat_history,
            },
            config=config,
        )
        # 中枢页面用下面的"依据"列表展示来源，正文里的 [1] 编号反而会让人找不到对应面板
        return {
            "rag_result": strip_citations(result.get("answer") or ""),
            "rag_sources": result.get("sources") or [],
        }
    except Exception as err:
        print(f"[ragNode] {err}")
        return {"rag_result": "查询知识库时出错", "rag_sources": []}
