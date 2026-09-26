"""答案接地（groundedness）校验：防止模型编造订单数据

背景（线上真实事故）：
    用户发 "U-108"，订单 Agent 这次**没有调用工具**，直接编了一张订单表：
    ORD-003 已发货 299 元 智能手环 B5 …（真实数据里 ORD-003 是 U-101 的 iPhone 手机壳，
    而且根本不存在 U-108 这个用户）。

提示词里写了"不许编造"，但模型仍可能绕过工具直接生成——所以这里做**结构性兜底**：
凡是要给用户看的回答，里面出现的订单号必须能在工具返回的事实里找到，否则一律拦下。
"""
import json
import logging
import re
from typing import Iterable, Optional

logger = logging.getLogger("jisu.grounding")

ORDER_ID_RE = re.compile(r"ORD-\d+", re.IGNORECASE)
USER_ID_RE = re.compile(r"U-\d+", re.IGNORECASE)

# 命中后给用户的安全话术（宁可说没查到，也不能给错数据）
UNGROUNDED_ANSWER = (
    "亲，抱歉呀，这笔数据我这边没能核实到，为免给您错误信息就先不报了～"
    "麻烦您核对一下订单号（ORD-xxx）或用户 ID（U-xxx），确认后小购马上帮您查～"
)


def facts_text(*sources: Iterable) -> str:
    """把工具返回/查询结果拼成一段"事实文本"，供比对"""
    chunks = []
    for source in sources:
        if not source:
            continue
        if isinstance(source, str):
            chunks.append(source)
        elif isinstance(source, dict):
            chunks.append(json.dumps(source, ensure_ascii=False))
        elif isinstance(source, (list, tuple, set)):
            for item in source:
                if isinstance(item, dict):
                    chunks.append(json.dumps(item, ensure_ascii=False))
                else:
                    chunks.append(str(item))
        else:
            chunks.append(str(source))
    return "\n".join(chunks)


def _role_of(message) -> str:
    """兼容 dict / LangChain 消息两种形态取角色"""
    if isinstance(message, dict):
        return str(message.get("role") or "")
    role = str(getattr(message, "role", "") or "")
    if role:
        return role
    return {"ai": "assistant", "human": "user", "system": "system",
            "tool": "tool"}.get(str(getattr(message, "type", "") or ""), "")


def history_facts(history) -> str:
    """把本会话里"客服自己已经说过的内容"并入事实来源

    线上事故（Agent 页实测）：
        用户查完 U-103 的订单（ORD-008 / ORD-009）后只补一句 "U-103"，
        模型这一轮没调工具、直接沿用上文作答；出口校验只比对**本轮**工具事实，
        于是回答里的 ORD-008 被判成"编造"，整段被替换成"没能核实到" ——
        数据明明是前几轮真实查出来的。

    这些历史回答在产出当时都过了同一道校验，可以安全地当作事实来源；
    真正编造的**新**订单号依然拦得住。
    """
    chunks = []
    for message in history or []:
        if _role_of(message) != "assistant":
            continue
        content = (message.get("content") if isinstance(message, dict)
                   else getattr(message, "content", ""))
        if isinstance(content, str) and content:
            chunks.append(content)
    return "\n".join(chunks)


# 「格式示例」不是订单数据：模型热心告诉用户"订单号格式如 ORD-001"时，
# 整段回答被判成编造、换成"没能核实到"，用户一脸懵（线上实测，用户发一句看不懂的话就会遇到）。
# 只在两个条件同时成立时才放过：
#   1. 订单号紧跟在"格式 / 例如 / 比如"这类示例提示词后面；
#   2. 订单号后面没有跟着数据（金额、状态、商品…）—— 跟着数据的仍然是编造，照拦。
_EXAMPLE_CUE_RE = re.compile(r"(格式|例如|比如|示例|举例|像是|如)[^。！？；\n]{0,3}$")
_DATA_AFTER_RE = re.compile(
    r"^[^。！？；\n]{0,12}?(元|¥|金额|状态|已发货|已完成|待发货|已取消|退款中|已签收|签收|商品|件)"
)


def _is_format_example(answer: str, match: "re.Match") -> bool:
    """这个订单号只是"格式示例"，不是当成订单数据在用"""
    if not _EXAMPLE_CUE_RE.search(answer[: match.start()]):
        return False
    return not _DATA_AFTER_RE.match(answer[match.end():])


def ungrounded_order_ids(answer: str, facts: str) -> list[str]:
    """回答里出现了、但事实里没有的订单号（去掉"格式示例"这种非数据用法）"""
    if not answer:
        return []
    known = {oid.upper() for oid in ORDER_ID_RE.findall(facts or "")}
    offending: list[str] = []
    for match in ORDER_ID_RE.finditer(answer):
        order_id = match.group(0).upper()
        if order_id in known or order_id in offending:
            continue
        if _is_format_example(answer, match):
            continue
        offending.append(order_id)
    return sorted(offending)


def check_answer(answer: str, facts: str, *, require_facts_for_ids: bool = True) -> tuple[bool, dict]:
    """校验回答是否"接地"

    返回 (是否通过, 详情)。不通过的情况：
      1. 回答里的订单号在工具事实里找不到（典型的编造）
      2. 工具明确返回错误/无数据，但回答却报出了订单号

    只校验订单号——它是最强、最可靠的信号（格式固定、可精确比对）；
    金额/状态这类自由文本不做硬校验，避免误伤正常措辞。
    """
    if not require_facts_for_ids or not answer:
        return True, {}

    offending = ungrounded_order_ids(answer, facts)
    if offending:
        detail = {"reason": "order_id_not_in_facts", "offending": offending}
        logger.error("答案接地校验失败：回答里出现事实中不存在的订单号 %s", offending)
        return False, detail
    return True, {}


def sanitize(answer: str, facts: str, route: str = "", trace_id: str = "") -> tuple[str, Optional[dict]]:
    """不接地就替换成安全话术，返回 (最终回答, 事故详情)"""
    ok, detail = check_answer(answer, facts)
    if ok:
        return answer, None
    detail.update({"route": route, "trace_id": trace_id, "answer_preview": (answer or "")[:200]})
    return UNGROUNDED_ANSWER, detail
