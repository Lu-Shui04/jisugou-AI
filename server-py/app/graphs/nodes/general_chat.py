"""智能中枢的闲聊分支

只走这一条时（intents == ["general"]），答案在这里就产出了。
提示词在 app/prompts/graph.py（graph_chat_prompt）。
"""
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableConfig

from app.models.deepseek import create_model
from app.prompts.graph import graph_chat_prompt

# streaming=True：闲聊分支的答案也是直接给用户的，需要逐 token 输出
_chain = graph_chat_prompt | create_model(temperature=0.7, streaming=True) | StrOutputParser()


def general_chat_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]
    messages = state.get("messages") or []
    chat_history = [
        ("human" if m.type == "human" else "assistant", m.content) for m in messages[-8:]
    ]

    result = _chain.invoke(
        {"user_input": user_input, "chat_history": chat_history}, config=config
    )
    return {"final_answer": result}
