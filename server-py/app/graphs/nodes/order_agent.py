from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.tools.order_tools import all_tools, deterministic_lookup

_model = create_model(temperature=0)
_agent_app = create_react_agent(
    model=_model,
    tools=all_tools,
    prompt="""你是极速购的订单查询助手。

规则：
1. 必须调用工具拿数据，绝对不能自己编造订单号、物流单号、金额、商品或状态
2. 只要用户给了订单号（ORD-xxx）或用户 ID（U-xxx），就直接调用对应工具查询；查订单列表用 getUserOrders，查单笔用 getOrderInfo，查物流用 getLogisticsInfo
3. 如果用户只给了一个 ID（比如 "U-103"），结合上文的对话判断是要查订单列表还是查某笔订单，然后调用工具
4. 工具返回 error 时，把 error 里的事实**原样转达**：
   - "用户 U-109 不存在" 就说不存在，**不要改写成"暂无订单"**、不要猜"可能没下过单"
   - "订单 ORD-999 不存在" 就说不存在
   - 返回里有 hint 字段时，按 hint 引导用户（例如告诉他可用的用户 ID 范围）
5. **禁止推测和编造**：不要说"可能是…或者…"这类没有依据的猜测，只陈述工具返回的事实
6. 用户问题里出现"我"且上文已经提供过用户 ID，就用上文那个 ID 去查
7. 不要因为查不到就转而索要订单号，除非用户确实问的是订单
8. 只有用户**明确要求现在就办**退款/退货、改收货地址、投诉时，才告诉他拨打人工售后专线
   400-888-8888（工作日 9:00-18:00），且不要索要订单号/用户 ID；
   **只是咨询规则、时效、流程时正常回答，不要提人工电话**（不该提的地方提了，像在推脱）

只查询数据，不需要生成最终的客服回答。""",
)


def order_agent_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]

    # 带上最近几轮对话：用户可能只是回答了"U-103"这种补充信息
    history = list(state.get("messages") or [])[-8:]
    if history and (getattr(history[-1], "content", "") or "").strip() == user_input.strip():
        history = history[:-1]

    try:
        result = _agent_app.invoke(
            {"messages": history + [HumanMessage(content=user_input)]}, config=config
        )

        # 从消息列表提取工具调用步骤
        msgs = result["messages"]
        steps = []
        for i, msg in enumerate(msgs):
            tool_calls = getattr(msg, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    obs = msgs[i + 1].content if i + 1 < len(msgs) else ""
                    steps.append({"tool": tc["name"], "input": tc["args"], "obs": obs})

        final_msg = msgs[-1]

        # 兜底：一个字都没调工具，却给了答案 → 很可能是编的，改用确定性查询的事实
        if not steps:
            fallback = deterministic_lookup(user_input)
            if fallback:
                print(f"[orderAgentNode] 模型未调用工具，已改用确定性查询："
                      f"{[s['tool'] for s in fallback['steps']]}")
                return {"order_result": fallback}

        return {"order_result": {"answer": final_msg.content, "steps": steps}}
    except Exception as err:
        print(f"[orderAgentNode] {err}")
        return {"order_result": {"answer": "查询订单信息时出错", "steps": []}}
