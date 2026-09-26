"""ID 解析：把文本里的订单号 / 用户 ID / 快递单号抠出来

原来是写在 app/tools/order_tools.py 里的私有正则，权限模块（app/security/access.py）
也要用同一套规则做「这段话有没有提到别人的订单」的判断 —— 两边各写一份迟早会跑偏，
所以统一收在这里。

注意用前后「不接字母数字」的断言代替 \b：中文紧跟在 ID 后面（"u103给我看下物流"）时，
中文算 \w，\b 匹配不上，ID 会被整条漏掉（线上真有人这么发）。
"""
import re

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


def any_id(text: str) -> bool:
    """文本里有没有业务 ID（没有 ID 就没法做确定性查询）"""
    return bool(ORDER_ID_RE.search(text or "") or USER_ID_RE.search(text or "")
                or TRACKING_RE.search(text or ""))
