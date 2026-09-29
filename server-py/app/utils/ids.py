"""ID 解析：把文本里的订单号 / 用户 ID / 快递单号抠出来

原来是写在 app/tools/order_tools.py 里的私有正则，权限模块（app/security/access.py）
也要用同一套规则做「这段话有没有提到别人的订单」的判断 —— 两边各写一份迟早会跑偏，
所以统一收在这里。

注意用前后「不接字母数字」的断言代替 \b：中文紧跟在 ID 后面（"u103给我看下物流"）时，
中文算 \w，\b 匹配不上，ID 会被整条漏掉（线上真有人这么发）。
"""
import re

from app.utils.messages import message_content

# 用户不一定规规矩矩写 "U-103"：线上真有人发 "u103"、"ord006"（小写、漏掉连字符）
ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9])ORD-?(\d+)(?!\d)", re.IGNORECASE)
USER_ID_RE = re.compile(r"(?<![A-Za-z0-9])U-?(\d+)(?!\d)", re.IGNORECASE)
TRACKING_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2}\d{8,})(?!\d)", re.IGNORECASE)


def normalize_id(value) -> str:
    """ID 统一成大写并去掉空格，避免用户输入 u-100 / U 100 查不到"""
    return (value or "").strip().upper().replace(" ", "")


def _canonical_ids(prefix: str, text: str, pattern: re.Pattern) -> list[str]:
    """从文本里抠出规范化后的 ID（补回连字符、去重、保持出现顺序）"""
    return list(dict.fromkeys(f"{prefix}-{digits}" for digits in pattern.findall(text or "")))


def order_ids(text: str) -> list[str]:
    return _canonical_ids("ORD", text, ORDER_ID_RE)


def user_ids(text: str) -> list[str]:
    return _canonical_ids("U", text, USER_ID_RE)


def tracking_nos(text: str) -> list[str]:
    return [match.upper() for match in TRACKING_RE.findall(text or "")]


def referenced_ids(text: str) -> dict:
    """一段文本里提到的全部业务 ID（权限校验与确定性查询共用）"""
    return {
        "orders": order_ids(text),
        "users": user_ids(text),
        "trackings": tracking_nos(text),
    }


# ── 省略写法与指代：用户不会每次都把订单号写全 ──────────────────────
# 线上实测：用户看完订单一览后追问「004为什么没有下单时间」——
# "004" 抠不出 ORD 号，模型只能凭上文记忆作答，然后被出口接地校验拦成"没能核实到"。
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NTH_RE = re.compile(r"第\s*([一二三四五六七八九十两]|\d{1,2})\s*(?:笔|个|条|单|张|件)")
_BARE_DIGITS_RE = re.compile(r"(?<![A-Za-z0-9])(\d{3,5})(?!\d)")
_ANAPHORA_RE = re.compile(r"(这单|那单|这笔|那笔|这个订单|那个订单|该订单|此单|刚才那|上面那|它)")
_ORDER_TOPIC_RE = re.compile(r"(订单|物流|快递|发货|到哪|签收|派送|退款)")


def history_order_ids(history) -> list[str]:
    """上文里提到过的订单号（按出现顺序去重）"""
    chunks = [message_content(item) for item in (history or [])]
    return order_ids("\n".join(chunk for chunk in chunks if chunk))


def resolve_order_ids(text: str, history=None, valid=()) -> list[str]:
    """把"省略写法 / 指代"补全成完整订单号；补不出来返回空列表

    只认**上文里真的出现过**、或者（给了 valid 时）**确实存在**的订单号：
    补不出来就返回空 —— 宁可让模型反问一句，也不能拿一个猜的号去查。
    多种解释同时命中（例如 "004" 同时对得上两个已知号）时也返回空，不赌。
    """
    text = text or ""
    direct = order_ids(text)
    if direct:
        return direct

    # 上下文里的号（按出现顺序，用于"第一笔"这类序号指代）
    context = history_order_ids(history)
    # 候选集 = 上下文里的号 ∪ 本人名下的号
    # 后者是必须的：线上那次表格里恰好漏掉了 ORD-004，只认上下文就补不出来
    candidates = list(dict.fromkeys(context + [normalize_id(item) for item in (valid or [])]))
    if not candidates:
        return []

    picked: list[str] = []

    # 1) 后几位数字："004" / "#004" —— 要求唯一命中，多个候选就不猜
    matches: list[str] = []
    for digits in _BARE_DIGITS_RE.findall(text):
        for order_id in candidates:
            if order_id.split("-")[-1].lstrip("0") == digits.lstrip("0") and order_id not in matches:
                matches.append(order_id)
    if len(matches) == 1:
        picked.append(matches[0])

    # 2) 序号指代："第一笔" / "第2单"（按上下文里出现的顺序数）
    ordered = context or candidates
    nth = _NTH_RE.search(text)
    if nth:
        token = nth.group(1)
        index = int(token) if token.isdigit() else _CN_NUM.get(token, 0)
        if 1 <= index <= len(ordered):
            picked.append(ordered[index - 1])

    # 3) 指代 + 订单话题："这单到哪了" / "它发货了吗"
    if not picked and _ANAPHORA_RE.search(text) and _ORDER_TOPIC_RE.search(text):
        picked.append(ordered[-1])

    return list(dict.fromkeys(picked))
