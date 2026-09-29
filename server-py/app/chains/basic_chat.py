"""
Chain 链式调用
使用 LCEL（LangChain Expression Language）管道语法
将 Prompt → Model → OutputParser 串联
"""
from langchain_core.output_parsers import StrOutputParser

from app.models.deepseek import create_model
from app.prompts.customer_service import customer_service_prompt

# ─── 极速购客服 Chain（非流式）───────────────────────────────────
_model = create_model(temperature=0.5)
_parser = StrOutputParser()

customer_service_chain = customer_service_prompt | _model | _parser

# ─── 极速购客服 Chain（流式）────────────────────────────────────
_streaming_model = create_model(temperature=0.5, streaming=True)

customer_service_stream_chain = customer_service_prompt | _streaming_model | _parser

def format_history(history=None):
    """将前端传来的 { role, content } 数组转换为 LangChain 消息格式"""
    history = history or []
    result = []
    for msg in history:
        if msg.get("role") == "user":
            result.append(("human", msg.get("content")))
        elif msg.get("role") == "assistant":
            result.append(("assistant", msg.get("content")))
    return result
