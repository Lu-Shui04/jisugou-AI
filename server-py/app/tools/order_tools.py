"""订单工具：数据权限的最后一公里

工具是唯一能碰到订单数据的地方，所以**权限校验必须落在这里**：
只在路由层挡是挡不住的 —— 提示词注入能让模型拿别人的订单号去调工具，
ReAct 也可能"热心"地换个 ID 再试一次。

每个工具进来先做三件事：
    1. 有没有身份（匿名 / 令牌过期 → UNAUTHENTICATED，一个字段都不给）
    2. 要查的东西是不是本人的（不是 → FORBIDDEN，并记一条越权事件）
    3. 才轮到查数据
真实用户 ID 只有一个来源：app/security/identity.current_principal()（由令牌解析而来）。
入参里的 userId 只是"用户声称要查谁"，永远不参与鉴权。
"""
import json

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.data.mock import logistics
from app.security import access, identity
from app.resilience.tool_guard import resilient_tool
from app.utils.ids import normalize_id, order_ids, resolve_order_ids, tracking_nos, user_ids


class OrderIdInput(BaseModel):
    orderId: str = Field(description="订单号，格式为 ORD-xxx，例如 ORD-001")


class TrackingNoInput(BaseModel):
    trackingNo: str = Field(description="快递单号，例如 SF1234567890")


class UserIdInput(BaseModel):
    userId: str = Field(
        default="",
        description='用户 ID，格式为 U-xxx。只能填**当前登录用户本人**的 ID（系统会校验，'
                    '填别人的会被拒绝）；不知道时留空，默认查本人。',
    )


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def known_user_ids() -> list[str]:
    """系统里真实存在的用户 ID（账号表；U-104 是"有账号、没订单"的用户）"""
    return identity.known_user_ids()


def _user_hint() -> str:
    ids = known_user_ids()
    if not ids:
        return "系统内暂无任何用户"
    return f"系统内现有用户 ID：{ids[0]} ~ {ids[-1]}（共 {len(ids)} 个）"


# 每个工具都套了一层 超时 + 重试 + 降级（app/tools/resilience.py）：
# 外部系统超时/抖动时自动重试，最终失败返回可读的 error JSON，不打断 Agent 链路
@tool(
    "getOrderInfo",
    args_schema=OrderIdInput,
    description="根据订单号查询订单详情，包括订单状态、商品列表、金额、快递信息。"
                "只能查当前登录用户本人的订单，他人的订单一律返回无权查看。"
                "当用户询问订单状态、订单内容时调用。",
)
@resilient_tool("getOrderInfo")
def get_order_info_tool(orderId: str) -> str:
    principal = identity.current_principal()

    # 1) 身份：没有身份就没有数据
    if principal.anonymous:
        return _dumps(access.deny_unauthenticated(principal))

    order_id = normalize_id(orderId)
    order = access.find_order(order_id)
    if not order:
        # 明确区分"订单不存在"，不要含糊成"查不到"，否则模型只能猜
        return _dumps(
            {
                "error": f"订单 {order_id} 不存在",
                "hint": "订单号格式为 ORD-xxx。请如实告知用户该订单号不存在并让他核对，"
                        "不要猜测订单内容、也不要编造状态。",
            }
        )

    # 2) 归属：别人的订单，连同状态金额一起不给
    if not access.owns_order(principal, order_id):
        return _dumps(access.deny_foreign_order(principal, order_id))

    return _dumps(order)


@tool(
    "getLogisticsInfo",
    args_schema=TrackingNoInput,
    description="根据快递单号查询物流轨迹，包括各节点时间、地点、状态。"
                "只能查当前登录用户本人包裹的物流，他人的包裹一律返回无权查看。"
                "当用户询问快递到哪了、物流状态时调用。",
)
@resilient_tool("getLogisticsInfo")
def get_logistics_tool(trackingNo: str) -> str:
    principal = identity.current_principal()
    if principal.anonymous:
        return _dumps(access.deny_unauthenticated(principal))

    tracking_no = normalize_id(trackingNo)
    records = logistics.get(tracking_no)

    # 快递单号能从订单上反查到归属：不校验的话，绕过订单号直接报单号就能看别人物流
    if access.find_order_by_tracking(tracking_no):
        if not access.owns_tracking(principal, tracking_no):
            return _dumps(access.deny_foreign_tracking(principal, tracking_no))

    if not records:
        return _dumps(
            {
                "error": f"快递单号 {tracking_no} 不存在",
                "hint": "请如实告知用户该单号查不到物流，让他核对单号；"
                        "由订单号查出的物流信息更可靠。",
            }
        )
    return _dumps({"trackingNo": tracking_no, "records": records})


@tool(
    "getUserOrders",
    args_schema=UserIdInput,
    description='查询**当前登录用户本人**的订单列表摘要。用户问"我有哪些订单"、"最近的订单"时调用；'
                "userId 留空即查本人，填别人的 ID 会被系统拒绝。",
)
@resilient_tool("getUserOrders")
def get_user_orders_tool(userId: str = "") -> str:
    principal = identity.current_principal()
    if principal.anonymous:
        return _dumps(access.deny_unauthenticated(principal))

    # 关键：**忽略**入参里的 userId 作为过滤条件，只认令牌里的真实用户 ID。
    # 传了别人的 ID 不是"帮他查"，而是拒绝 —— 这正是这次补的洞。
    requested = normalize_id(userId)
    if requested and not access.owns_user(principal, requested):
        return _dumps(access.deny_foreign_user(principal, requested))

    user_id = principal.user_id

    # 「用户不存在」和「用户没有订单」是两回事，不能混成一句"暂无订单"，
    # 否则模型拿不到事实，只能猜"可能没下过单、或者 ID 有误"（线上真实出现过）。
    if user_id not in known_user_ids():
        return _dumps(
            {
                "error": f"用户 {user_id} 不存在",
                "knownUsers": known_user_ids(),
                "hint": _user_hint() + "。请直接告诉用户该用户 ID 不存在、让他核对，"
                        "不要猜测他有没有下过单，也不要编造订单。",
            }
        )

    user_orders = access.orders_of(user_id)
    if not user_orders:
        return _dumps({"error": f"用户 {user_id} 名下暂无订单"})
    summary = [
        {
            "orderId": order["orderId"],
            "status": order["status"],
            "amount": order["amount"],
            "createTime": order["createTime"],
            # 带上商品名：用户接着问"这是什么商品"时才有上下文依据
            "items": [f"{item['name']}×{item['qty']}" for item in order["items"]],
        }
        for order in user_orders
    ]
    return _dumps(summary)


all_tools = [get_order_info_tool, get_logistics_tool, get_user_orders_tool]


# ── 确定性查询兜底 ──────────────────────────────────────────────
# 线上事故：用户发 "U-108"，ReAct 这次没调工具，直接编了一张订单表
# （ORD-003 已发货 299 元 智能手环 B5 …），事实来源完全不明。
# 所以只要用户输入里出现 ID，就允许调用方绕开模型自己查一遍。
# 注意：确定性查询同样走上面那三个工具，因此**同样受权限约束** ——
# 用户报别人的订单号，这里查出来的也是"无权查看"，不会因为"绕开模型"就漏数据。
def deterministic_lookup(text: str, principal: identity.Principal | None = None,
                        history=None) -> dict | None:
    """把文本里的 ID 抠出来直接查工具，返回 {"steps": [...], "answer": "<工具原始事实>"}

    没有 ID 时返回 None（那就没法确定性查询，只能引导用户提供 ID）。

    除了写全的 ORD-xxx / U-xxx / 快递单号，还认**省略写法与指代**：
    用户看完订单一览后追问「004为什么没有下单时间」「第二笔到哪了」「这单发货了吗」，
    这些抠不出 ID，模型只能凭上文记忆作答（然后被出口接地校验拦成"没能核实到"）。
    这里借助 history 把号补全 —— 补不出唯一结果就返回 None，不猜。
    """
    order_id_list = order_ids(text)
    if not order_id_list:
        # 兜底候选集：上文出现过的订单号 + 本人名下的订单号（后者让"新会话直接问 004"也能补全）
        own_orders = []
        current = principal or identity.current_principal()
        if not current.anonymous:
            own_orders = [item["orderId"] for item in access.orders_of(current.user_id)]
        order_id_list = resolve_order_ids(text, history, valid=own_orders)
    user_id_list = user_ids(text)
    tracking_list = tracking_nos(text)
    if not (order_id_list or user_id_list or tracking_list):
        return None

    principal = principal or identity.current_principal()

    steps: list[dict] = []
    facts: list[str] = []
    # 在"当前身份"下执行：即便调用方是后台任务，也不会跑成匿名或别人的身份
    with identity.principal_scope(principal):
        for order_id in dict.fromkeys(order_id_list):
            observation = get_order_info_tool.invoke({"orderId": order_id})
            steps.append({"tool": "getOrderInfo", "input": {"orderId": order_id}, "obs": observation})
            facts.append(observation)
        for user_id in dict.fromkeys(user_id_list):
            observation = get_user_orders_tool.invoke({"userId": user_id})
            steps.append({"tool": "getUserOrders", "input": {"userId": user_id}, "obs": observation})
            facts.append(observation)
        for tracking_no in dict.fromkeys(tracking_list):
            observation = get_logistics_tool.invoke({"trackingNo": tracking_no})
            steps.append({"tool": "getLogisticsInfo", "input": {"trackingNo": tracking_no},
                          "obs": observation})
            facts.append(observation)

    if not facts:
        return None
    return {"steps": steps, "answer": "；".join(facts)}
