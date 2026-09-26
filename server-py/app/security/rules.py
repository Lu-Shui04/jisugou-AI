"""提示词安全：规则层 + 业务白名单

设计原则（配合 layers）：
1. 白名单 / 规则层是**零 token、毫秒级**的，能拦掉的就不调小模型（省钱、也快）
2. 规则只做「高置信度」拦截，宁可漏给模型判，也不要误杀正常问句
3. 知识库上下文同样要洗：检索到的文档里也可能藏着注入指令

规则来源参考 OWASP LLM01（Prompt Injection）的缓解建议：
输入校验、指令与数据分离、最小权限、输出过滤。
"""
import re

# ── 白名单：正常的购物业务问句（命中且不含攻击词 → 直接放行，不调模型）────
BUSINESS_PATTERNS = [
    r"ORD-\d+",
    r"U-\d+",
    r"\bSF\d{6,}\b",
    r"订单|下单|发货|收货|签收|物流|快递|运单|包裹|配送|运费|包邮|到货|催单",
    r"退款|退货|换货|售后|保修|维修|质保|三包|无理由|七天|7天|退换",
    r"发票|开票|专票|普票|税号",
    r"价格|多少钱|报价|优惠|折扣|优惠券|满减|积分|会员|支付|付款|花呗|分期|微信支付|支付宝",
    r"商品|产品|规格|参数|续航|材质|颜色|尺码|库存|型号|配件|包装|说明书",
    r"蓝牙耳机|键盘|手机壳|钢化膜|充电器|显示器|支架|耳机|手表|鼠标",
    r"客服|人工|热线|电话|在线时间|营业时间|投诉",
    r"你好|您好|在吗|谢谢|多谢|辛苦|再见|拜拜|好的|嗯嗯|哈哈|请问",
]

# ── 追问白名单：很短、纯承接上文的回话（"是的""两个都要""都查一下"）──────
# 线上事故：用户在问订单物流，回了一句"两个都要"；小模型只看这 4 个字、没有上文，
# 把它判成了"提示词攻击"（layer=model category=attack），而且结论进了 24h 缓存 ——
# 此后这句话再也过不去，用户再说什么都被拦。
# 这类回话本身没有任何攻击面，零 token 直接放行。
# 安全性由两点保证：
#   1. 必须命中「承接词表」开头（都要 / 是的 / 都查 …）；
#   2. 后面只能再跟语气助词等填充字符，而这些字符里**没有**任何
#      攻击用的动词/名词（忽略、输出、扮演、提示词、系统、指令、规则…都拼不出来）。
FOLLOWUP_HEADS = (
    # 确认 / 同意
    "是的", "对", "对的", "没错", "正确", "好", "好的", "好吧", "行", "可以", "都行", "随便",
    # 承接上一轮：全都要 / 一起 / 继续 / 第几个
    "嗯", "哦", "噢", "要", "都要", "全要", "全都", "都", "两个都", "两个都要", "俩都要",
    "一起", "一起查", "一起看", "继续", "接着", "然后呢", "还有呢", "还有吗", "再来",
    "就这个", "就它", "这个", "那个", "第一个", "第二个", "第三个",
)
# 允许跟在承接词后面的填充字符（语气助词 / 常用后缀），故意不含任何攻击词用字
FOLLOWUP_TAILS = "啊呀哦吧呢么了的下看查来一趟个个一嘛哈嗯好的要全都~！。，、？ ！?"

MAX_FOLLOWUP_CHARS = 14

FOLLOWUP_RE = re.compile(
    "^(?:" + "|".join(re.escape(head) for head in sorted(FOLLOWUP_HEADS, key=len, reverse=True)) + ")"
    + "[" + re.escape(FOLLOWUP_TAILS) + "]{0,6}$"
)


def is_followup(text: str) -> bool:
    """很短、纯承接上文的回话（本身没有攻击面）"""
    value = (text or "").strip()
    if not value or len(value) > MAX_FOLLOWUP_CHARS:
        return False
    return bool(FOLLOWUP_RE.match(value))


# ── 攻击规则（高置信度，命中即拦截，不花 token）─────────────────────
ATTACK_RULES: list[tuple[str, str]] = [
    # 忽略 / 覆盖既有指令
    ("ignore_instructions",
     r"(忽略|无视|忘记|忘掉|不要管|不用管|抛开|跳出|清除|清空|取消)[^。！？；\n]{0,12}"
     r"(上面|以上|之前|前面|先前|所有|全部|一切|你(的)?)?[^。！？；\n]{0,8}"
     r"(指令|指示|规则|设定|设定|提示|要求|限制|约束|人设|角色|身份)"),
    ("ignore_instructions_en",
     r"(?i)\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,30}\b(previous|prior|above|earlier|all|system)\b[^.\n]{0,20}\b(instruction|prompt|rule|message|guideline)"),

    # 套取系统提示词 / 内部规则
    ("leak_system_prompt",
     r"((系统|初始|原始|内部|隐藏|完整)?\s*(提示词|prompt|system\s*prompt|系统指令|系统设定|内部规则|预设)"
     r"[^。！？\n]{0,14}(输出|打印|复述|重复|原样|完整|展示|告诉|给我看|发我|翻译|转成|是什么|内容|泄露|吐出来|说出来))"
     r"|((输出|打印|复述|重复|原样|完整|展示|告诉|给我看|泄露)[^。！？\n]{0,10}(你的|你的全部)?\s*(提示词|prompt|系统指令|设定))"
     r"|(repeat|print|show|reveal|output|leak)[^.\n]{0,20}(your|the)\s+(system\s+)?(prompt|instructions|rules)"
     # 元指代：提到"上面的/之前的…规则、设定、提示、内容"本身就是套取内部指令的信号
     r"|((上面|以上|之前|前面|刚才|先前)(说|写|给|提到|给出)?的?(所有|全部)?\s*(规则|设定|指令|要求|提示|内容|人设))"),

    # 角色覆盖 / 越狱
    ("role_override",
     r"(你现在是|从现在开始你是|从现在起你是|扮演|假装你是|假装成|假定你是|你应该扮演)"
     r"|(没有|不受|无需|不再)\s*(任何)?\s*(限制|约束|规则|审查|过滤)"
     r"|(无限制|不受限制|无审查|越狱|开发者模式|上帝模式|DAN\b|jailbreak|developer\s*mode|no\s*restrictions?)"),

    # 伪造系统 / 开发者指令
    ("fake_system_tag",
     r"(\[\s*(system|developer|admin|assistant|系统|管理员)\s*\])"
     r"|(<\|\s*(im_start|im_end|system|endoftext)\s*\|>)"
     r"|(#+\s*(system|instruction|指令|系统提示))"
     r"|(新指令[：:]|新规则[：:]|以下指令优先|本条.{0,6}优先)"
     r"|((接下来|现在|请|你)?\s*(完全)?\s*(按|照)\s*(我|我的)\s*(说|要求|指令)\s*的?\s*(做|来|执行))"),

    # 绕过安全 / 权限
    ("bypass_safety",
     r"(绕过|跳过|关闭|解除|禁用|取消)[^。！？\n]{0,8}(校验|验证|审核|检查|限制|安全|规则|过滤|权限|风控)"),

    # 编码 / 变形载荷
    ("encoded_payload",
     r"(base64|rot13|十六进制|hex)\s*(解码|decode|解密|转换)?[^。！？\n]{0,12}(执行|运行|eval|照做|按此)"
     r"|\b(decode|eval|execute)\b[^.\n]{0,20}base64"),

    # 套取别的用户数据
    ("other_user_data",
     r"(其他|别的|别人|所有|全部|任意)[^。！？\n]{0,6}(用户|账号|客户)[^。！？\n]{0,8}(订单|数据|信息|手机号|地址)"),
]

# ── 可疑词（不足以直接判攻击，但会让白名单失效、送去小模型判）──────────
SUSPICIOUS_HINTS = (
    "指令", "规则", "设定", "人设", "角色", "身份", "提示词", "prompt", "system",
    "忽略", "无视", "忘记", "扮演", "假装", "越狱", "限制", "开发者", "管理员",
    "绕过", "跳过", "解除", "禁用", "校验", "越权", "权限",
    "其他用户", "别的用户", "所有用户", "全部用户", "全部订单", "所有订单",
    "系统配置", "内部配置", "原始设定", "翻译成英文发",
    "instruction", "ignore", "jailbreak", "dan", "override",
)

# ── 输出侧：系统提示词泄露的特征串 ──────────────────────────────
LEAK_OUTPUT_MARKERS = (
    "你是极速购电商平台", "只回答与购物", "不要编造订单信息",
    "BUSINESS_PATTERNS", "ATTACK_RULES", "系统提示词如下", "我的系统提示是",
)

_COMPILED_ATTACK = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in ATTACK_RULES]
_COMPILED_BIZ = [re.compile(pattern, re.IGNORECASE) for pattern in BUSINESS_PATTERNS]


def normalize(text: str) -> str:
    """归一化：去掉空白与控制字符，防止用空格/换行拆分关键词"""
    text = (text or "").replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def match_attack(text: str) -> tuple[str, str] | None:
    """命中攻击规则返回 (规则名, 命中的片段)"""
    for name, pattern in _COMPILED_ATTACK:
        found = pattern.search(text)
        if found:
            return name, found.group(0)[:60]
    return None


def is_business(text: str) -> bool:
    """是否命中购物业务白名单"""
    return any(pattern.search(text) for pattern in _COMPILED_BIZ)


def has_suspicious_hint(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in SUSPICIOUS_HINTS)


def whitelisted(text: str) -> bool:
    """白名单快速放行条件：命中业务词 + 没有可疑词 + 长度可控

    任何一条不满足就走下一步（规则 / 小模型），避免被"订单号 + 注入指令"绕过。
    """
    return is_business(text) and not has_suspicious_hint(text)


def sanitize_context(text: str) -> tuple[str, list[str]]:
    """清洗知识库上下文：剔掉文档里夹带的指令型句子

    返回 (清洗后的文本, 命中的规则名列表)。只做减法，不改写原意。
    """
    if not text:
        return text, []

    hits: list[str] = []
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and (match_attack(stripped) or _looks_like_instruction(stripped)):
            name = (match_attack(stripped) or ("instruction_like", ""))[0]
            hits.append(name)
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines), hits


def _looks_like_instruction(line: str) -> bool:
    """文档里出现的"请忽略以上内容""系统指令："这类句式"""
    return bool(re.search(
        r"^(请|现在|接下来|注意)[^。\n]{0,6}(忽略|无视|忘记|执行|扮演|输出)"
        r"|(系统|开发者|管理员)\s*(指令|提示|消息)\s*[：:]",
        line, re.IGNORECASE,
    ))


def leaked_system_prompt(answer: str) -> str:
    """输出里是否出现系统提示词特征串，返回命中的特征（没有则空串）"""
    for marker in LEAK_OUTPUT_MARKERS:
        if marker in (answer or ""):
            return marker
    return ""
