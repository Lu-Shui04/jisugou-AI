"""订单节点（智能中枢里的 order 分支）

两个和安全相关的点：
1. 身份从**图状态**里拿（路由把令牌解析出的真实用户 ID 放进了 state），
   拿不到才退回请求上下文。这个节点跑在子线程里，不能只指望 ContextVar。
2. 整段执行（含 ReAct 里每一次工具调用、确定性查询兜底）都包在 principal_scope 里，
   工具读到的"当前登录用户"因此永远是同一个真实用户。

系统提示词全文在 app/prompts/order_agent.py。
"""
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.prompts import order_agent as order_agent_prompts
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
    who = order_agent_prompts.identity_line(
        authenticated=principal.authenticated,
        user_id=principal.user_id,
        user_name=principal.user_name,
    )
    rules = order_agent_prompts.order_agent_rules(who)
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
                fallback = deterministic_lookup(user_input, principal, history=state.get("messages"))
                if fallback:
                    print(f"[orderAgentNode] 模型未调用工具，已改用确定性查询："
                          f"{[s['tool'] for s in fallback['steps']]}")
                    return {"order_result": fallback}

            return {"order_result": {"answer": final_msg.content, "steps": steps}}
        except Exception as err:
            print(f"[orderAgentNode] {err}")
            return {"order_result": {"answer": "查询订单信息时出错", "steps": []}}
