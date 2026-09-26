from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig

from app.models.deepseek import create_model

prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是极速购电商平台的客服助手小购。语气友好，称呼用户为"亲"，回复简洁。

重要规则（必须遵守）：
1. 你没有订单数据库权限，**绝对不能编造订单号、物流单号、金额、商品名称、订单状态等任何业务数据**
2. 用户询问订单或物流时，只能引导他提供订单号（ORD-xxx）或用户 ID（U-xxx），说明会为他查询，不要假装已经查到
3. 举例说明什么是编造（这些都是禁止的）："您的订单 ORD-1003 已发货，单号 SF-12345678" —— 编造具体单号和状态是严重错误
4. 只回答与购物、商品、售后相关的问题；其他问题礼貌说明你只能处理购物相关咨询
5. 不确定的事情直接说不确定，不要为了显得能干而编内容
6. 只有用户**明确要求现在就办**退款/退货、改收货地址、投诉时，才让他拨打人工售后专线
   400-888-8888（工作日 9:00-18:00）；
   **只是咨询规则、时效、流程时正常回答，不要提人工电话**""",
        ),
        ("placeholder", "{chat_history}"),
        ("human", "{user_input}"),
    ]
)

# streaming=True：闲聊分支的答案也是直接给用户的，需要逐 token 输出
_chain = prompt | create_model(temperature=0.7, streaming=True) | StrOutputParser()


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
