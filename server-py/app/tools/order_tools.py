import json
import re

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.data.mock import logistics, orders
from app.tools.resilience import resilient_tool


class OrderIdInput(BaseModel):
    orderId: str = Field(description="订单号，格式为 ORD-xxx，例如 ORD-001")


class TrackingNoInput(BaseModel):
    trackingNo: str = Field(description="快递单号，例如 SF1234567890")


class UserIdInput(BaseModel):
    userId: str = Field(description="用户 ID，格式为 U-xxx，例如 U-100")


def _normalize_id(value: str) -> str:
    """ID 统一成大写并去掉空格，避免用户输入 u-100 / U 100 查不到"""
    return (value or "").strip().upper().replace(" ", "")


def known_user_ids() -> list[str]:
    """系统里真实存在的用户 ID（从订单数据归纳）"""
    return sorted({order["userId"] for order in orders.values()})


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
    description="根据订单号查询订单详情，包括订单状态、商品列表、金额、快递信息。当用户询问订单状态、订单内容时调用。",
)
@resilient_tool("getOrderInfo")
def get_order_info_tool(orderId: str) -> str:
    order_id = _normalize_id(orderId)
    order = orders.get(order_id)
    if not order:
        # 明确区分"订单不存在"，不要含糊成"查不到"，否则模型只能猜
        return json.dumps(
            {
                "error": f"订单 {order_id} 不存在",
                "hint": "订单号格式为 ORD-xxx。请如实告知用户该订单号不存在并让他核对，"
                        "不要猜测订单内容、也不要编造状态。",
            },
            ensure_ascii=False,
        )
    return json.dumps(order, ensure_ascii=False)


@tool(
    "getLogisticsInfo",
    args_schema=TrackingNoInput,
    description="根据快递单号查询物流轨迹，包括各节点时间、地点、状态。当用户询问快递到哪了、物流状态时调用。",
)
@resilient_tool("getLogisticsInfo")
def get_logistics_tool(trackingNo: str) -> str:
    tracking_no = _normalize_id(trackingNo)
    records = logistics.get(tracking_no)
    if not records:
        return json.dumps(
            {
                "error": f"快递单号 {tracking_no} 不存在",
                "hint": "请如实告知用户该单号查不到物流，让他核对单号；"
                        "由订单号查出的物流信息更可靠。",
            },
            ensure_ascii=False,
        )
    return json.dumps({"trackingNo": tracking_no, "records": records}, ensure_ascii=False)


@tool(
    "getUserOrders",
    args_schema=UserIdInput,
    description='根据用户 ID 查询该用户的所有订单列表摘要。当用户询问"我有哪些订单"、"最近的订单"时调用。',
)
@resilient_tool("getUserOrders")
def get_user_orders_tool(userId: str) -> str:
    user_id = _normalize_id(userId)
    known = known_user_ids()

    # 关键：「用户不存在」和「用户没有订单」是两回事，不能混成一句"暂无订单"，
    # 否则模型拿不到事实，只能猜"可能没下过单、或者 ID 有误"（线上真实出现过）。
    if user_id not in known:
        return json.dumps(
            {
                "error": f"用户 {user_id} 不存在",
                "knownUsers": known,
                "hint": _user_hint() + "。请直接告诉用户该用户 ID 不存在、让他核对，"
                        "不要猜测他有没有下过单，也不要编造订单。",
            },
            ensure_ascii=False,
        )

    user_orders = [o for o in orders.values() if o["userId"] == user_id]
    if not user_orders:
        return json.dumps({"error": f"用户 {user_id} 名下暂无订单"}, ensure_ascii=False)
    summary = [
        {
            "orderId": o["orderId"],
            "status": o["status"],
            "amount": o["amount"],
            "createTime": o["createTime"],
            # 带上商品名：用户接着问"这是什么商品"时才有上下文依据
            "items": [f"{item['name']}×{item['qty']}" for item in o["items"]],
        }
        for o in user_orders
    ]
    return json.dumps(summary, ensure_ascii=False)


all_tools = [get_order_info_tool, get_logistics_tool, get_user_orders_tool]


# ── 确定性查询兜底 ──────────────────────────────────────────────
# 线上事故：用户发 "U-108"，ReAct 这次没调工具，直接编了一张订单表
# （ORD-003 已发货 299 元 智能手环 B5 …），事实来源完全不明。
# 所以只要用户输入里出现 ID，就允许调用方绕开模型自己查一遍。
# 用户不一定规规矩矩写 "U-103"：线上真有人发 "u103"、"ord006"（小写、漏掉连字符），
# 这种写法以前抠不出 ID，等于白查。所以连字符可选、大小写不敏感。
# 注意用前后「不接字母数字」的断言代替 \b：中文紧跟在 ID 后面（"u103给我看下物流"）时，
# 中文算 \w，\b 匹配不上，ID 会被整条漏掉。
_ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9])ORD-?(\d+)(?!\d)", re.IGNORECASE)
_USER_ID_RE = re.compile(r"(?<![A-Za-z0-9])U-?(\d+)(?!\d)", re.IGNORECASE)
_TRACKING_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{8,})(?!\d)", re.IGNORECASE)


def _canonical_ids(prefix: str, text: str, pattern: "re.Pattern") -> list[str]:
    """从文本里抠出规范化后的 ID（补回连字符、去重、保持出现顺序）"""
    return list(dict.fromkeys(f"{prefix}-{digits}" for digits in pattern.findall(text or "")))


def deterministic_lookup(text: str) -> dict | None:
    """把文本里的 ID 抠出来直接查工具，返回 {"steps": [...], "answer": "<工具原始事实>"}

    没有 ID 时返回 None（那就没法确定性查询，只能引导用户提供 ID）。
    """
    order_ids = _canonical_ids("ORD", text, _ORDER_ID_RE)
    user_ids = _canonical_ids("U", text, _USER_ID_RE)
    tracking_nos = [m.upper() for m in _TRACKING_RE.findall(text or "")]
    if not (order_ids or user_ids or tracking_nos):
        return None

    steps: list[dict] = []
    facts: list[str] = []
    for order_id in dict.fromkeys(order_ids):
        observation = get_order_info_tool.invoke({"orderId": order_id})
        steps.append({"tool": "getOrderInfo", "input": {"orderId": order_id}, "obs": observation})
        facts.append(observation)
    for user_id in dict.fromkeys(user_ids):
        observation = get_user_orders_tool.invoke({"userId": user_id})
        steps.append({"tool": "getUserOrders", "input": {"userId": user_id}, "obs": observation})
        facts.append(observation)
    for tracking_no in dict.fromkeys(tracking_nos):
        observation = get_logistics_tool.invoke({"trackingNo": tracking_no})
        steps.append({"tool": "getLogisticsInfo", "input": {"trackingNo": tracking_no}, "obs": observation})
        facts.append(observation)

    if not facts:
        return None
    return {"steps": steps, "answer": "；".join(facts)}
