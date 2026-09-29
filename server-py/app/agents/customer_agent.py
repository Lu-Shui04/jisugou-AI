from datetime import datetime

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.prompts import order_agent as order_agent_prompts
from app.security import identity
from app.tools.order_tools import all_tools

# streaming=True：Agent 页要逐 token 出字（工具调用轮的内容会被前端当作过程提示）
_model = create_model(temperature=0, streaming=True)


def _system_prompt(state=None) -> list:
    """动态系统提示：把当前登录用户告诉模型

    提示词全文在 app/prompts/order_agent.py；真正的拦截在工具里
    （app/tools/order_tools.py 每次调用都校验归属），
    这里只是让模型知道"我是谁"，别去问用户"请提供用户 ID"，也别去猜。
    """
    principal = identity.current_principal()
    who = order_agent_prompts.identity_line(
        authenticated=principal.authenticated,
        user_id=principal.user_id,
        user_name=principal.user_name,
    )
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    rules = order_agent_prompts.customer_agent_rules(who, now)

    messages = list(state.get("messages") or []) if isinstance(state, dict) else []
    return [SystemMessage(content=rules)] + messages


def create_customer_agent():
    return create_react_agent(
        model=_model,
        tools=all_tools,
        prompt=_system_prompt)
