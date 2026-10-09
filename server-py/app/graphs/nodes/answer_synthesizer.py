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
from app.security import access, identity
from app.utils import grounding
from app.utils.ids import resolve_single_order

# streaming=True：合成节点是最终答案产出者，开流式才能被 LangGraph 逐 token 推给前端
_model = create_model(temperature=0.5, streaming=True)
_parser = StrOutputParser()
_chain_asked = synthesizer_prompt_asked | _model | _parser
_chain_not_asked = synthesizer_prompt_not_asked | _model | _parser


def _reference_line(state, user_input: str) -> str:
    """把用户这句里的指代（"另一个""这单"）确定性解析成订单号，写进合成提示

    没有这一步时：工具明明已经查回 ORD-003 的完整物流，合成节点仍会写
    "小购没把握是哪一笔，方便把订单号发给我吗"（线上实测，同一句"另一个呢"多次里有多次这样）。
    解析只认**本人**订单，解析不出唯一结果就返回空串（宁可不提示，也不猜）。
    """
    principal = identity.current_principal()
    if isinstance(state, dict):
        user = identity.get_user(str(state.get("authenticated_user_id") or ""))
        if user is not None:
            principal = identity.Principal(user_id=user.user_id, user_name=user.name,
                                           authenticated=True, reason="ok")
    if not principal.authenticated:
        return ""
    own = [order["orderId"] for order in access.orders_of(principal.user_id)]
    picked = resolve_single_order(user_input, (state or {}).get("messages"), valid=own)
    if not picked:
        return ""
    return (f"\n\n【指代解析】用户这句话里的指代已经确定为 **{picked}**"
            f"（他本人订单里「不是刚提到的那一笔」）：直接按这一笔回答，不要再反问他指哪一笔。")


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
            "reference": _reference_line(state, user_input),
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
