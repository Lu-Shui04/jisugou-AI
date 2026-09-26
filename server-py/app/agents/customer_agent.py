from datetime import datetime

from langgraph.prebuilt import create_react_agent

from app.models.deepseek import create_model
from app.tools.order_tools import all_tools

# streaming=True：Agent 页要逐 token 出字（工具调用轮的内容会被前端当作过程提示）
_model = create_model(temperature=0, streaming=True)


def _system_prompt():
    now = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
    return f"""你是极速购电商平台的智能客服助手小购。

回答规则：
1. 需要查询数据时，先调用对应工具获取真实数据，不要猜测或编造
2. 语气友好，称呼用户为"亲"
3. 拿到数据后用自然语言组织回答，不要直接粘贴 JSON
4. 如果用户没有提供订单号但需要查询，先询问订单号
5. 工具返回 error 时**原样转达事实**（"用户 U-109 不存在"就说不存在），不要改写成"暂无订单"，
   也不要说"可能是…"这类推测；返回里有 hint 字段就按 hint 引导（例如可用 ID 范围）

当前时间：{now}"""


def create_customer_agent():
    return create_react_agent(
        model=_model, 
        tools=all_tools, 
        prompt=_system_prompt())
