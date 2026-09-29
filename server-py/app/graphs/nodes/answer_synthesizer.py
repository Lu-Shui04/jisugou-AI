"""答案合成节点：把订单查询结果 / 知识库结果合成最终答复

提示词在 app/prompts/graph.py（问订单和不问订单两份，只差第 6 条规则）。
这里额外做一件事：出口接地校验 —— 答复里的订单号必须在工具事实里找得到，
否则说明模型在编订单，直接换成安全话术。
"""
import json

from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableConfig

from app.models.deepseek import create_model
from app.prompts.graph import synthesizer_prompt_asked, synthesizer_prompt_not_asked
from app.utils import grounding

# streaming=True：合成节点是最终答案产出者，开流式才能被 LangGraph 逐 token 推给前端
_model = create_model(temperature=0.5, streaming=True)
_parser = StrOutputParser()
_chain_asked = synthesizer_prompt_asked | _model | _parser
_chain_not_asked = synthesizer_prompt_not_asked | _model | _parser


def answer_synthesizer_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]
    order_result = state.get("order_result")
    rag_result = state.get("rag_result")
    final_answer = state.get("final_answer")
    intents = state.get("intents") or [state.get("intent") or "general"]

    # 只走 general 分支时，generalChatNode 已经生成好答案，直接透传
    if intents == ["general"] and final_answer:
        return {"final_answer": final_answer}

    order_facts = (
        json.dumps(order_result["answer"], ensure_ascii=False) if order_result else "无"
    )

    chain = _chain_asked if "order" in intents else _chain_not_asked
    result = chain.invoke(
        {
            "user_input": user_input,
            "order_result": order_facts,
            "rag_result": rag_result or "无",
        },
        config=config,
    )

    # 出口接地校验：回答里的订单号必须在工具事实（或用户自己说的）里找得到，
    # 否则说明模型在编订单——直接换成安全话术（线上出现过编造整张订单表的情况）。
    # 本会话前面已经核实过的回答同样算事实来源，否则用户追问上一轮查到的订单
    # 会被误判成编造、整段换成"没能核实到"。
    facts = grounding.facts_text(
        order_facts, rag_result, user_input, grounding.history_facts(state.get("messages"))
    )
    result, incident = grounding.sanitize(result, facts, route="graph")
    if incident:
        print(f"[answerSynthesizer] 接地校验失败，已拦截编造回答：{incident.get('offending')}")

    return {"final_answer": result}
