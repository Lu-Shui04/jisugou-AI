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


def ungrounded_order_ids(answer: str, facts: str) -> list[str]:
    """回答里出现了、但事实里没有的订单号"""
    if not answer:
        return []
    known = {oid.upper() for oid in ORDER_ID_RE.findall(facts or "")}
    return sorted({oid.upper() for oid in ORDER_ID_RE.findall(answer) if oid.upper() not in known})


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
