"""订单节点（智能中枢里的 order 分支）

两个和安全相关的点：
1. 身份从**图状态**里拿（路由把令牌解析出的真实用户 ID 放进了 state），
   拿不到才退回请求上下文。这个节点跑在子线程里，不能只指望 ContextVar。
2. 整段执行（含 ReAct 里每一次工具调用、确定性查询兜底）都包在 principal_scope 里，
   工具读到的"当前登录用户"因此永远是同一个真实用户。
"""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.security import identity
from app.tools.order_tools import all_tools, deterministic_lookup

_model = create_model(temperature=0)


def principal_from_state(state) -> identity.Principal:
    """本次请求的身份：优先用状态里的真实用户 ID（路由放进去的），其次看上下文"""
    if isinstance(state, dict):
        user = identity.get_user(str(state.get("authenticated_user_id") or ""))
        if user is not None:
            return identity.Principal(user_id=user.user_id, user_name=user.name,
                                      authenticated=True, reason="ok")
    return identity.current_principal()


def _prompt(state) -> list:
    """动态系统提示：把"当前登录用户是谁"告诉模型（真正的拦截在工具里，这里只是让它少走弯路）"""
    principal = principal_from_state(state)
    if principal.authenticated:
        who = (f"当前登录用户：{principal.user_id}（{principal.user_name}）。"
               f"系统只会返回**他自己**的订单数据，查别人的一律被拒绝。")
    else:
        who = ("当前**没有**登录用户身份：所有订单数据都查不到，"
               "直接请用户到页面左上角选择用户身份。")
    rules = f"""你是极速购的订单查询助手。

{who}

规则：
1. 必须调用工具拿数据，绝对不能自己编造订单号、物流单号、金额、商品或状态
2. 只要用户给了订单号（ORD-xxx）或用户 ID（U-xxx），就直接调用对应工具查询；查订单列表用 getUserOrders，查单笔用 getOrderInfo，查物流用 getLogisticsInfo
3. 如果用户只给了一个 ID（比如 "U-103"），结合上文的对话判断是要查订单列表还是查某笔订单，然后调用工具
4. 工具返回 error 时，把 error 里的事实**原样转达**：
   - "用户 U-109 不存在" 就说不存在，**不要改写成"暂无订单"**、不要猜"可能没下过单"
   - "订单 ORD-999 不存在" 就说不存在
   - **code=FORBIDDEN（无权查看）**：这是"这条数据不是他的"，如实告诉他只能查询本人订单，
     **不要**改口成"不存在"、**不要**换个 ID 或订单号重试、**不要**猜内容
   - **code=UNAUTHENTICATED**：请他先到页面左上角选择用户身份
   - 返回里有 hint 字段时，按 hint 引导用户（例如告诉他可用的用户 ID 范围）
5. **禁止推测和编造**：不要说"可能是…或者…"这类没有依据的猜测，只陈述工具返回的事实
6. 用户问题里出现"我"时，用当前登录用户的 ID 去查（就是上面写的那个），不要反问他是谁
7. 不要因为查不到就转而索要订单号，除非用户确实问的是订单
8. 只有用户**明确要求现在就办**退款/退货、改收货地址、投诉时，才告诉他拨打人工售后专线
   400-888-8888（工作日 9:00-18:00），且不要索要订单号/用户 ID；
   **只是咨询规则、时效、流程时正常回答，不要提人工电话**（不该提的地方提了，像在推脱）
9. 用户问"我的订单到哪里了 / 到哪了 / 物流"时：先用 getUserOrders 找出**已发货**的订单，
   再逐笔调 getOrderInfo 拿快递单号、调 getLogisticsInfo 查轨迹，把轨迹事实一起返回；
   不要只返回订单列表、也不要反问"您想查哪一个"
10. 工具返回里**有的字段就是事实**：自己整理时漏掉的字段（例如 createTime 下单时间），
   要么重新调工具核对后补上，要么直说"上一条漏填了"；
   **不许**说成"系统没有返回 / 摘要里没有这个字段"
11. 用户提到**某一笔具体订单**时（哪怕只写"004""第一笔"），必须先调 getOrderInfo 查一遍再回答，
   不要凭上文记忆作答——出口接地校验只认本轮工具返回的事实，凭记忆答会被拦成"没能核实到"

只查询数据，不需要生成最终的客服回答。"""
    messages = list(state.get("messages") or []) if isinstance(state, dict) else []
    return [SystemMessage(content=rules)] + messages


_agent_app = create_react_agent(model=_model, tools=all_tools, prompt=_prompt)


def order_agent_node(state, config: RunnableConfig = None):
    user_input = state["user_input"]
    principal = principal_from_state(state)

    # 带上最近几轮对话：用户可能只是回答了"U-103"这种补充信息
    history = list(state.get("messages") or [])[-8:]
    if history and (getattr(history[-1], "content", "") or "").strip() == user_input.strip():
        history = history[:-1]

    with identity.principal_scope(principal):
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
                fallback = deterministic_lookup(user_input, principal)
                if fallback:
                    print(f"[orderAgentNode] 模型未调用工具，已改用确定性查询："
                          f"{[s['tool'] for s in fallback['steps']]}")
                    return {"order_result": fallback}

            return {"order_result": {"answer": final_msg.content, "steps": steps}}
        except Exception as err:
            print(f"[orderAgentNode] {err}")
            return {"order_result": {"answer": "查询订单信息时出错", "steps": []}}
