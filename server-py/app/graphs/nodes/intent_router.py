import re

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from app.models.deepseek import create_model

intent_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是一个意图分类器。

请结合「最近对话」和「用户最新输入」，判断用户最新一句话涉及的意图。
**一句话里可以同时涉及多个意图**，例如：
- "帮我查一下订单 ORD-009，还有你们商品能不能开发票" → order,knowledge
- "这个多少钱，能开发票吗" → order,knowledge

分类词：
- order：询问订单状态、订单列表、物流信息、退款进度；**或者用户在补充订单号（ORD-xxx）/ 用户 ID（U-xxx）以配合上一轮的订单查询**
- knowledge：商品介绍、规格参数、价格、售后政策、退换货规则、发票、配送运费、支付优惠等能从知识库查到的问题；
  **退款/退货的时效与规则也属于 knowledge**（"退款需要多少天""几天到账""退货有什么规则""运费谁出"），
  只有查"我这一单的退款进度/退款状态"才算 order
- general：其他类型的对话、闲聊、无法归类的问题

判断要点（很重要）：
1. 用户的最新输入经常是**追问**，很短、或者只有几个字（例如"这是什么商品""多少钱""几天到""哪一笔""要多久"）。
   追问必须结合最近对话的**主题**来判断，不能只看这几个字：
   - 最近聊的是**订单 / 物流 / 退款进度** → 追问涉及 order
   - 最近聊的是**商品参数 / 售后政策** → 追问涉及 knowledge
   - 用户补充订单号（ORD-xxx）或用户 ID（U-xxx）→ order
2. 只有明确在问商品介绍、规格、价格、政策条款时，才是 knowledge
3. 完全脱离购物场景的闲聊（天气、写代码、闲聊）才是 general
4. 一句话里用"还有 / 顺便 / 另外 / 再问一下"等连接了两个不同问题，且分属不同分类时，**必须都返回**
5. 拿不准时宁可多返回一个分类，也不要漏掉用户问到的部分

最近对话：
{history}

只输出分类词，多个用英文逗号分隔（例如：order,knowledge），不要任何解释""",
        ),
        ("human", "{user_input}"),
    ]
)

_chain = intent_prompt | create_model(temperature=0) | StrOutputParser()

VALID_INTENTS = ["order", "knowledge", "general"]

# 意图 → 图节点
INTENT_NODES = {"order": "orderAgent", "knowledge": "ragNode", "general": "generalChat"}


def _parse_intents(raw: str) -> list[str]:
    """把模型输出解析成意图列表（支持 order / order,knowledge / order knowledge 等写法）"""
    tokens = re.split(r"[,\s，、/|]+", (raw or "").lower())
    intents: list[str] = []
    for token in tokens:
        if token in VALID_INTENTS and token not in intents:
            intents.append(token)
    # 有明确意图时就不需要 general 兜底了
    if len(intents) > 1 and "general" in intents:
        intents.remove("general")
    return intents or ["general"]


def _history_text(state, limit: int = 6) -> str:
    """把最近几轮对话拼成上下文，解决"只回一个用户 ID"被误判成闲聊的问题"""
    messages = state.get("messages") or []
    lines = []
    for message in messages[-limit:]:
        role = "用户" if getattr(message, "type", "") == "human" else "客服"
        content = (getattr(message, "content", "") or "").strip().replace("\n", " ")
        if content:
            lines.append(f"{role}：{content[:150]}")
    return "\n".join(lines) or "（无，这是第一句话）"


# 带这些指代的短问句属于"追问"，按上文主题归位
_FOLLOWUP_REFERENCE = (
    "这个", "那个", "它", "这单", "那单", "这笔", "那笔", "哪一笔", "哪一单",
    "多少钱", "什么商品", "是什么", "什么时候到", "几天到", "怎么退",
)


def _recent_topic_is_order(state, limit: int = 6) -> bool:
    """最近几轮是不是在聊订单（看订单号/用户 ID/订单相关词）"""
    messages = state.get("messages") or []
    recent = messages[-limit:-1]  # 最后一条是当前这句话，排除掉
    text = " ".join((getattr(m, "content", "") or "") for m in recent)
    if re.search(r"ORD-\d+", text) or re.search(r"U-\d+", text):
        return True
    return any(word in text for word in ("订单", "物流", "快递", "发货", "退款"))


def intent_router_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]

    # 上游（退款意图判定）已经确定意图时直接用，不再花一次模型调用 —— 也避免它再判错
    preset = state.get("intents")
    if preset:
        clean = [item for item in preset if item in VALID_INTENTS] or ["general"]
        print(f'[intentRouter] "{user_input}" 使用预置意图 {",".join(clean)}（跳过模型判定）')
        return {"intent": clean[0], "intents": clean}

    history = _history_text(state)

    # 传 config 让 Token 统计的 callbacks 能透传到模型调用
    raw = _chain.invoke({"user_input": user_input, "history": history}, config=config)
    intents = _parse_intents(raw)

    # 追问兜底：上一轮在聊订单，"这是什么商品""多少钱"这类短追问必须带上 order
    # （订单里的商品/金额只有订单工具查得到，只丢给知识库必然查不到）
    if _recent_topic_is_order(state) and any(word in user_input for word in _FOLLOWUP_REFERENCE):
        if "order" not in intents:
            intents = ["order"] + [item for item in intents if item != "general"]
            print(f'[intentRouter] "{user_input}" 追问继承上文订单主题，补上 order')

    print(f'[intentRouter] "{user_input}" → {",".join(intents)}')
    return {"intent": intents[0], "intents": intents}


def route_by_intent(state):
    """返回一个或多个节点名，LangGraph 会并行执行多出来的分支

    例：intents = ["order", "knowledge"] → ["orderAgent", "ragNode"]
    两个分支各自写 order_result / rag_result，最后一起交给 answerSynthesizer。
    """
    intents = state.get("intents") or [state.get("intent") or "general"]
    nodes: list[str] = []
    for item in intents:
        node = INTENT_NODES.get(item, "generalChat")
        if node not in nodes:
            nodes.append(node)
    return nodes or ["generalChat"]
