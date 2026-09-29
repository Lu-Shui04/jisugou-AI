"""消息与历史相关的公共小工具

原来有三份"取消息文本"和两份"拼最近几轮对话"，行为还不完全一致；
统一收到这里，谁要谁 import，避免哪天只改了一处。
"""
from langchain_core.messages import AIMessage, HumanMessage


def to_lc_messages(history: list[dict]):
    """把 [{"role": "user"|"assistant", "content": "..."}] 转成 LangChain 消息列表"""
    return [
        HumanMessage(content=m.get("content") or "")
        if m.get("role") == "user"
        else AIMessage(content=m.get("content") or "")
        for m in (history or [])
    ]


def message_content(message) -> str:
    """取消息正文，兼容 dict（前端传来的历史）与 LangChain 消息两种形态"""
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", "") or "")


def history_text(state, limit: int = 6, empty: str = "") -> str:
    """把最近几轮对话拼成一段纯文本，给"改写问题 / 判意图"这类纯文本提示用

    state 传图状态（dict，取 messages）或直接传消息列表都可以；没内容时返回 empty。
    """
    messages = state.get("messages") if isinstance(state, dict) else state
    lines = []
    for message in (messages or [])[-limit:]:
        role = "用户" if getattr(message, "type", "") == "human" else "客服"
        content = message_content(message).strip().replace("\n", " ")
        if content:
            lines.append(f"{role}：{content[:150]}")
    return "\n".join(lines) or empty
