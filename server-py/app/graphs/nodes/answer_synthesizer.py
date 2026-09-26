import json

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from app.models.deepseek import create_model
from app.utils import grounding

_BASE_RULES = """你是极速购电商平台的客服助手小购。

根据以下查询结果，为用户生成一个清晰、友好的回答。
称呼用户为"亲"，语气专业，内容简洁准确。

**重要规则**：
1. 只能使用下面「订单查询结果」里真实存在的数据，**绝对不要编造订单号、物流单号、金额、商品或状态**
2. 知识库结果为"无"时，如实告诉用户没查到，不要凭常识作答
3. 如果**同时**给了订单查询结果和知识库查询结果，说明用户一句话里问了两件事：
   两部分都要回答到，先答订单/物流，再答商品或政策问题，一个都不能漏
4. 不要在回答里输出 [1] 这类引用编号（编号只在知识库问答页面使用）
5. 只回答用户真正问到的内容，不要主动扩展话题、不要添加无关的追问；
   尤其**不要**用"需要小购帮您查别的吗""还有什么可以帮您"这类客套句收尾
6. **禁止推测**：查询结果里没有的信息就不要提，不要说"可能是…""或许是…""也有可能…"，
   不要把"用户不存在"说成"可能没下过单"；事实是什么就说什么
7. 查询结果里带 hint 字段时，按 hint 引导用户（例如告诉可用的示例 ID）
8. 只有当用户**明确要求现在就办**退款/退货、改收货地址、投诉时，才告诉他拨打人工售后专线
   400-888-8888（工作日 9:00-18:00），并且不要反问他订单号/用户 ID；
   **用户只是在咨询规则、时效、条件、流程、能不能办时，正常回答即可，不要提人工电话**，
   也不要主动推销人工渠道 —— 不该提的地方提了，用户会觉得在推脱"""

# 只有用户确实问了订单/物流时，才允许"引导提供订单号"
# （之前无条件带上这条规则，导致问"耳机咋卖"也会被反问订单号，体验很怪）
_ORDER_RULE_WHEN_ASKED = """
6. 用户这次问的是订单/物流：如果订单查询结果为空、显示"无"或显示 error，
   不要给出任何订单详情，引导他提供订单号（ORD-xxx）或用户 ID（U-xxx）"""

_ORDER_RULE_WHEN_NOT_ASKED = """
6. 用户这次**没有**问订单/物流：即使「订单查询结果」为空，也**绝对不要**反问他订单号或用户 ID，
   直接回答他问的商品/政策问题即可"""

_prompt_asked = ChatPromptTemplate.from_messages([
    ("system", _BASE_RULES + _ORDER_RULE_WHEN_ASKED + "\n\n订单查询结果（如有）：{order_result}\n知识库查询结果（如有）：{rag_result}"),
    ("human", "{user_input}"),
])

_prompt_not_asked = ChatPromptTemplate.from_messages([
    ("system", _BASE_RULES + _ORDER_RULE_WHEN_NOT_ASKED + "\n\n订单查询结果（如有）：{order_result}\n知识库查询结果（如有）：{rag_result}"),
    ("human", "{user_input}"),
])

# streaming=True：合成节点是最终答案产出者，开流式才能被 LangGraph 逐 token 推给前端
_model = create_model(temperature=0.5, streaming=True)
_parser = StrOutputParser()
_chain_asked = _prompt_asked | _model | _parser
_chain_not_asked = _prompt_not_asked | _model | _parser


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
