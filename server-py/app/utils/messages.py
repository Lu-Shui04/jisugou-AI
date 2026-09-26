"""会话历史 -> LangChain 消息 的转换工具"""
from langchain_core.messages import AIMessage, HumanMessage


def to_lc_messages(history: list[dict]):
    """把 [{"role": "user"|"assistant", "content": "..."}] 转成 LangChain 消息列表"""
    return [
        HumanMessage(content=m.get("content") or "")
        if m.get("role") == "user"
        else AIMessage(content=m.get("content") or "")
        for m in (history or [])
    ]
