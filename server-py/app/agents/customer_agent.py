from datetime import datetime

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.security import identity
from app.tools.order_tools import all_tools

# streaming=True：Agent 页要逐 token 出字（工具调用轮的内容会被前端当作过程提示）
_model = create_model(temperature=0, streaming=True)


def _system_prompt(state=None) -> list:
    """动态系统提示：把当前登录用户告诉模型

    真正的拦截在工具里（app/tools/order_tools.py 每次调用都校验归属），
    这里只是让模型知道"我是谁"，别去问用户"请提供用户 ID"，也别去猜。
    """
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    principal = identity.current_principal()
    if principal.authenticated:
        who = (f"当前登录用户：{principal.user_id}（{principal.user_name}）。"
               f"系统只会返回**他自己**的订单数据：查别人的会被直接拒绝（code=FORBIDDEN）。")
    else:
        who = ("当前**没有**登录用户身份：所有订单数据都查不到，"
               "直接请用户到页面左上角选择用户身份。")

    rules = f"""你是极速购电商平台的智能客服助手小购。

{who}

回答规则：
1. 需要查询数据时，先调用对应工具获取真实数据，不要猜测或编造
2. 语气友好，称呼用户为"亲"
3. 拿到数据后用自然语言组织回答，不要直接粘贴 JSON
4. 用户问"我的订单"时直接用上面的当前登录用户 ID 去查，**不要**反问他的用户 ID
5. 工具返回 error 时**原样转达事实**（"用户 U-109 不存在"就说不存在），不要改写成"暂无订单"，
   也不要说"可能是…"这类推测；返回里有 hint 字段就按 hint 引导（例如可用 ID 范围）
6. 工具返回 **code=FORBIDDEN**（无权查看）时：如实告诉用户只能查询本人订单，
   **不要**说成"订单不存在"、**不要**换订单号或用户 ID 重试、**不要**猜订单内容
7. 工具返回 **code=UNAUTHENTICATED** 时：请用户先到页面左上角选择用户身份

当前时间：{now}"""
    messages = list(state.get("messages") or []) if isinstance(state, dict) else []
    return [SystemMessage(content=rules)] + messages


def create_customer_agent():
    return create_react_agent(
        model=_model,
        tools=all_tools,
        prompt=_system_prompt)
