"""退款类意图的判定与「人工接力」策略

两条线上真实反馈（都是被真实用户打脸的）：
    1. "我说退款，它问我一大堆，最后告诉我办不了，气死我了。"
       —— 系统没有退款分支：意图识别把退款归到 order，订单 Agent 就追问订单号，
          问完工具只会查不会办，最后回一句"办不了"。先追问、后拒绝。
    2. "退款需要多少天？" 被回了一句"请联系人工"。
       —— 用关键词表判"是办还是问"，词表没写"多少天"，就把咨询当成了要办。

所以判定不再靠关键词表，改成**分层判定**（零 token 优先，灰区交小模型）：

    L0 规则快路径   零 token：整句明显与退款无关 → none；明显就是要办（"我要退款"）→ action
    L1 缓存         Redis：同样的输入不再花 token（默认 24h）
    L2 小模型       智谱 glm-4-air：灰区才调，输出 action / info / progress / mixed / none
    L3 失败兜底     **fail-open**：模型超时或报错一律走正常链路（不拦、不堵），只记日志

判定结果只决定"走哪条路"，不决定"答什么"：
    action   → 本模块给出人工通道话术（系统确实没有办理权限，这是唯一的确定性回答）
    info     → 走知识库检索（chat / agent 这两个没有知识库的入口，借用同一套检索链路）
    progress → 走订单工具查真实状态
    mixed    → 完全不拦，交给原链路（多意图）
    none     → 完全不拦

换句话说：**除了"用户就是要办退款"这一种情况，其它全部放行**，内容永远来自知识库或工具。
新增一类"人工才能办"的事，只需在 TOPICS 里加一条。
"""
import json
import logging
import re
import os
import time
from typing import Optional

from app.db import redis_client
from app.prompts.handoff import JUDGE_FEWSHOT, JUDGE_SYSTEM
from app.resilience import CircuitOpenError, get_breaker

logger = logging.getLogger("jisu.handoff")

# ── 人工售后通道（与 app/data/knowledge/policies.md「联系客服」保持一致，有单测盯着）──
HOTLINE = "400-888-8888"
HOTLINE_HOURS = "工作日 9:00-18:00"
ONLINE_HOURS = "在线客服 9:00-21:00"

# ── 判定层配置 ──────────────────────────────────────────────────
JUDGE_ENABLED = os.getenv("HANDOFF_JUDGE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
JUDGE_MODEL = os.getenv("HANDOFF_MODEL", "glm-4-air")
JUDGE_BASE_URL = (os.getenv("HANDOFF_BASE_URL") or os.getenv("SECURITY_BASE_URL")
                  or "https://open.bigmodel.cn/api/paas/v4")
JUDGE_TIMEOUT = float(os.getenv("HANDOFF_TIMEOUT_SECONDS", "3"))
JUDGE_MAX_TOKENS = int(os.getenv("HANDOFF_MODEL_MAX_TOKENS", "24"))
JUDGE_CACHE_TTL = int(os.getenv("HANDOFF_CACHE_TTL", "86400"))

# 熔断器名字（同名会复用同一个熔断器）
JUDGE_BREAKER = "judge:handoff"

VALID_KINDS = ("action", "info", "progress", "mixed", "none")

# ── 主题定义：只用来做规则快路径与话术，不承担"判定"职责 ──────────────
TOPICS: dict[str, dict] = {
    "refund": {
        "label": "退款 / 退货",
        "words": (
            "退款", "退货", "退换", "换货", "退钱", "返款", "退单", "退运费",
            "退掉", "退一下", "退了", "售后", "拒收",
            "能退", "可以退", "想退", "要退", "退吗", "退么", "退不了", "退不成",
            "无理由", "三包", "退款政策", "售后政策",
        ),
        "action": (
            "我要退", "我想退", "帮我退", "给我退", "申请退", "办理退", "发起退",
            "要求退", "现在退", "马上退", "直接退", "必须退", "退了吧", "退款吧",
            "退货吧", "我要申请", "帮我申请", "怎么退", "如何退", "怎样退",
            "怎么申请", "退钱", "返款", "退给我", "不想要了", "不要了", "退不了",
            "能退吗", "可以退吗", "能退么", "可以退么", "能退不", "可以退不",
        ),
        # 只有这几个是"不用问模型、明显就是要办"，其余一律交小模型判（防止把咨询当办理）
        "strong": (
            "我要退", "我想退", "帮我退", "给我退",
            "马上退", "直接退", "退了吧", "退款吧", "退货吧",
            "我要申请", "帮我申请", "退给我", "退钱", "返款",
            # 注意：这里**没有**"申请退 / 怎么退 / 能退吗" —— 这类是"问怎么办"，
            # 交给小模型判（多半是 info，由知识库把流程讲清楚，而不是直接甩人工电话）
        ),
        "policy": (
            "政策", "规则", "规定", "条款", "标准", "条件", "什么意思", "了解一下",
            "咨询一下", "支持退", "允许退", "运费谁", "谁来承担", "手续费", "流程",
        ),
        "timing": (
            "多少天", "要几天", "需要几天", "几天", "多久", "多长时间", "多少时间",
            "多少工作日", "几个工作日", "到账时间", "几号到账", "什么时候到账",
            "什么时候退", "几天能到", "多久到",
        ),
        "progress": (
            "进度", "到哪", "到账了吗", "还没到", "还没退", "退款状态", "退款到",
        ),
        "patterns": (
            r"(能不能|可不可以|能否)[^，。！？\n]{0,4}退",
            r"退(一下|个|了|掉|掉吧)[^，。！？\n]{0,6}(款|钱|货)?",
        ),
        "say": "我要退款",
        "why": "退款、退货这类**要实际操作**的事，小购这边没有办理权限",
        "chat_why": "小购是基础客服，**办理退款 / 退货需要人工帮您操作**",
        "facts": [
            "一般商品 7 天无理由退货，质量问题 15 天内免费退换",
            "退款原路返回，信用卡到账可能需要 3-5 个银行工作日",
        ],
        "policy_tail": "需要**办理**退款 / 退货的话",
    },
    "complaint": {
        "label": "投诉 / 售后纠纷",
        "words": ("投诉", "举报", "曝光", "差评", "消协", "工商", "起诉", "315"),
        "action": (), "policy": (), "timing": (), "progress": (),
        "strong": ("我要投诉", "我要举报", "我要曝光", "投诉你们", "投诉你们平台"),
        "patterns": (r"投诉|举报|曝光",),
        "say": "我要投诉",
        "why": "投诉受理需要人工登记跟进，小购这边没有受理权限",
        "chat_why": "小购是基础客服，**投诉需要人工登记受理**",
        "facts": [
            "人工会为您登记工单并跟进到底，不会让您重复描述",
            "紧急售后也可以在订单详情页留言，客服 2 小时内响应",
        ],
        "policy_tail": "需要**投诉 / 反馈**的话",
    },
    "address": {
        "label": "修改收货信息",
        "words": (
            "改地址", "修改地址", "换地址", "改收货", "修改收货", "改电话",
            "改手机号", "改号码", "地址写错", "地址填错", "地址错了", "改一下地址",
        ),
        "action": (), "policy": (), "timing": (), "progress": (),
        "strong": ("改地址", "修改地址", "换地址", "地址改一下", "把地址改", "改一下地址"),
        "patterns": (
            r"地址[^，。！？\n]{0,8}(改|换|错|修改)",
            r"(改|修改|换)[^，。！？\n]{0,4}(收货)?地址",
            r"(改|换)[^，。！？\n]{0,4}(电话|手机号|号码)",
        ),
        "say": "我要改收货地址",
        "why": "修改收货地址 / 联系方式这类操作，小购这边没有办理权限",
        "chat_why": "小购是基础客服，**修改收货信息需要人工帮您操作**",
        "facts": [
            "未发货的订单，人工可以直接帮您改地址",
            "已发货的订单，人工会联系快递尝试拦截改派",
        ],
        "policy_tail": "需要**修改收货信息**的话",
    },
}

_ORDER_RE = re.compile(r"ORD-\d+", re.IGNORECASE)
_USER_RE = re.compile(r"U-\d+", re.IGNORECASE)

_NOISE = (
    "帮我", "麻烦", "一下", "可以", "能不能", "请问", "谢谢", "亲", "小购",
    "客服", "人工", "办理", "申请", "我要", "我想", "还是", "就是", "这个",
    "那个", "我的", "你们", "怎么办", "吗", "呢", "吧", "哦", "啊", "呀",
)

_memory_cache: dict = {}


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:24]


async def _cache_get(text: str) -> Optional[dict]:
    key = "handoff:judge:" + _digest(text)
    try:
        raw = await redis_client.get_redis().get(key)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    item = _memory_cache.get(key)
    if item and item.get("expire_at", 0) > time.time():
        return item.get("value")
    return None


async def _cache_set(text: str, value: dict) -> None:
    key = "handoff:judge:" + _digest(text)
    try:
        await redis_client.get_redis().set(key, json.dumps(value, ensure_ascii=False), ex=JUDGE_CACHE_TTL)
    except Exception:
        pass
    if len(_memory_cache) > 500:
        _memory_cache.clear()
    _memory_cache[key] = {"value": value, "expire_at": time.time() + JUDGE_CACHE_TTL}


def _match_topic(text: str) -> Optional[dict]:
    """命中退款族主题时返回主题信息（topic / matched / 是否整句都在说这件事）"""
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    for topic, spec in TOPICS.items():
        hit = next((word for word in spec["words"] if word in lowered), "")
        if not hit:
            match = next((m.group(0) for p in spec.get("patterns", ())
                          if (m := re.search(p, lowered))), "")
            if not match:
                continue
            hit = match
        matched = next((w for w in spec.get("action", ()) if w in lowered), "") or hit
        return {
            "topic": topic,
            "dominant": _is_dominant(raw, spec),
            "order_id": (_ORDER_RE.search(raw).group(0).upper() if _ORDER_RE.search(raw) else ""),
            "user_id": (_USER_RE.search(raw).group(0).upper() if _USER_RE.search(raw) else ""),
            "matched": matched,
        }
    return None


def _is_dominant(text: str, spec: dict) -> bool:
    """整句话是不是"主要就在说这件事"

    "我要退款" → True（剩下的字没内容）
    "我要退款，顺便问下 X6 耳机多少钱" → False（还有别的问题，判成 mixed 更合适）
    """
    residue = text
    vocab = (spec.get("words", ()) + spec.get("action", ()) + spec.get("policy", ())
             + spec.get("timing", ()) + spec.get("progress", ()) + _NOISE)
    for word in vocab:
        residue = residue.replace(word, "")
    for pattern in spec.get("patterns", ()):
        residue = re.sub(pattern, "", residue)
    residue = _ORDER_RE.sub("", residue)
    residue = _USER_RE.sub("", residue)
    residue = re.sub(r"[\s，。！？~,.!?、；;:：\"'“”‘’（）()\[\]【】\-]+", "", residue)
    return len(residue) <= 6


def _rule_verdict(text: str) -> Optional[dict]:
    """零 token 快路径：只处理"明显是"和"明显不是"两头，灰区一律交小模型"""
    topic = _match_topic(text)
    if not topic:
        return {"kind": "none", "topic": "", "layer": "rule", "matched": "",
                "order_id": "", "user_id": "", "reason": "整句与退款族无关（零 token 放行）"}
    lowered = text.lower()
    spec = TOPICS[topic["topic"]]

    # 只有"明显就是要办"才走零 token 快路径：
    #   1. 命中 strong 词表；
    #   2. 句子里没有夹着政策 / 时效 / 进度类问法（"退款收到货后几天退钱" 不能被当成要办）；
    #   3. 整句主要就在说这件事（夹了别的问题 → 交给小模型判 mixed）。
    marker = next((w for w in spec.get("strong", ()) if w in lowered), "")
    if not marker:
        return None
    asking = [w for key in ("policy", "timing", "progress") for w in spec.get(key, ()) if w in lowered]
    if asking:
        return None
    if not topic["dominant"]:
        return None
    return {**topic, "kind": "action", "layer": "rule",
            "reason": "零 token 判定：明确要办理（命中 %s）" % marker}


async def classify(text: str) -> dict:
    """判定一句话该走哪条路：action / info / progress / mixed / none"""
    started = time.perf_counter()
    raw = (text or "").strip()

    def done(verdict: dict) -> dict:
        verdict["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return verdict

    if not raw:
        return done({"kind": "none", "topic": "", "matched": "", "order_id": "",
                     "user_id": "", "layer": "empty", "reason": "空输入"})

    quick = _rule_verdict(raw)
    if quick:
        return done(quick)

    topic = _match_topic(raw) or {}

    if not JUDGE_ENABLED:
        return done({**topic, "kind": "mixed", "layer": "disabled",
                     "reason": "小模型判定已关闭，按正常链路处理"})

    cached = await _cache_get(raw)
    if cached and cached.get("kind") in VALID_KINDS:
        return done({**topic, "kind": cached["kind"], "layer": "cache",
                     "model": cached.get("model", JUDGE_MODEL),
                     "reason": "命中缓存：此前判定为 %s" % cached["kind"]})

    verdict = await _judge(raw)
    kind = verdict.get("kind") if verdict.get("kind") in VALID_KINDS else "mixed"
    if verdict.get("ok"):
        await _cache_set(raw, {"kind": kind, "model": JUDGE_MODEL})
    return done({**topic, "kind": kind,
                 **{k: v for k, v in verdict.items() if k != "kind"}})


async def _judge(text: str) -> dict:
    """调用智谱小模型；任何异常都 fail-open（返回 mixed，走正常链路，绝不堵死）"""
    breaker = get_breaker(JUDGE_BREAKER)

    # 熔断优先判断：打开时直接 fail-open，连"有没有 Key"都不必看
    allowed, why = breaker.allow()
    if not allowed:
        logger.warning("退款意图判定熔断打开（%s），直接走正常链路", why)
        return {"kind": "mixed", "layer": "circuit_open", "ok": False, "model": JUDGE_MODEL,
                "reason": "判定小模型熔断打开（fail-open：走正常链路）"}

    api_key = (os.getenv("HANDOFF_ZHIPU_API_KEY") or os.getenv("SECURITY_ZHIPU_API_KEY")
               or os.getenv("ZHIPU_API_KEY") or "").strip()
    if not api_key:
        logger.warning("未配置小模型 API Key，退款意图判定直接走正常链路")
        return {"kind": "mixed", "layer": "error", "ok": False,
                "reason": "未配置小模型 API Key（fail-open：走正常链路）"}

    messages = [{"role": "system", "content": JUDGE_SYSTEM}]
    for user_text, assistant_text in JUDGE_FEWSHOT:
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": assistant_text})
    messages.append({"role": "user", "content": text[:400]})

    payload = {
        "model": JUDGE_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": JUDGE_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}

    try:
        import httpx

        # 熔断：判定小模型整体挂掉时不要再让每个请求等满 JUDGE_TIMEOUT，
        # 直接快速失败 —— 走既定的 fail-open（正常链路处理，不拦不堵）
        async with breaker.aguard(), httpx.AsyncClient(timeout=JUDGE_TIMEOUT) as client:
            response = await client.post(
                JUDGE_BASE_URL.rstrip("/") + "/chat/completions",
                json=payload, headers=headers,
            )
        response.raise_for_status()
        data = response.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "") or ""
        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"kind": str(content).strip().strip('"')}
        kind = str(parsed.get("kind") or "").strip().lower()
        if kind not in VALID_KINDS:
            kind = "mixed"
        return {"kind": kind, "layer": "model", "ok": True, "model": JUDGE_MODEL,
                "model_tokens": tokens, "reason": "小模型判定为 %s" % kind}
    except CircuitOpenError as err:
        logger.warning("退款意图判定熔断打开（fail-open 走正常链路）：%s", err)
        return {"kind": "mixed", "layer": "circuit_open", "ok": False, "model": JUDGE_MODEL,
                "reason": "判定小模型熔断打开（fail-open：走正常链路）"}
    except Exception as err:
        logger.warning("退款意图小模型判定失败（fail-open 走正常链路）：%s", err)
        return {"kind": "mixed", "layer": "error", "ok": False, "model": JUDGE_MODEL,
                "reason": "小模型不可用：%s（fail-open：走正常链路）" % err}


def decide(verdict: Optional[dict], page: str) -> dict:
    """统一决策：这个入口该怎么办

    返回 {"reply": 直接回复的话术 | None, "kb": 是否改用知识库检索, "preset_intents": 预置意图}
    page：chat / agent / rag / graph
    """
    if not verdict or verdict.get("kind") in (None, "none", "mixed"):
        return {"reply": None, "kb": False, "preset_intents": None}

    kind = verdict["kind"]

    if kind == "action":
        if page == "chat":
            return {"reply": chat_reply(verdict), "kb": False, "preset_intents": None}
        return {"reply": action_reply(verdict, extra_hint(verdict)), "kb": False,
                "preset_intents": None}

    if kind == "info":
        # 咨询类：一律让知识库回答（chat / agent 没有知识库页面，由 kb_bridge 借同一套检索）
        return {"reply": None, "kb": True,
                "preset_intents": ["knowledge"] if page == "graph" else None}

    if kind == "progress":
        # 问"我这一单"的进度，分两种情况（线上实测踩过）：
        #   用户报了订单号 / 用户 ID → 直接查订单工具，给真实状态；
        #   什么都没报 → 别反问他单号（这正是被骂的"问一圈"），
        #                用知识库回答"进度在哪儿看"，并留人工电话。
        has_id = bool(verdict.get("order_id") or verdict.get("user_id"))
        if page == "chat":
            return {"reply": chat_reply(verdict), "kb": False, "preset_intents": None}
        if has_id:
            return {"reply": None, "kb": False,
                    "preset_intents": ["order"] if page == "graph" else None}
        if page == "graph":
            return {"reply": None, "kb": False, "preset_intents": ["knowledge"]}
        if page == "agent":
            return {"reply": None, "kb": True, "preset_intents": None}
        return {"reply": None, "kb": False, "preset_intents": None}

    return {"reply": None, "kb": False, "preset_intents": None}


def should_short_circuit(verdict: Optional[dict]) -> bool:
    """是否由本模块直接回复（只有"就是要办"才直接回）"""
    return bool(verdict) and verdict.get("kind") == "action"


def extra_hint(verdict: Optional[dict]) -> str:
    """混合意图时补一句指路，别把人家的其他问题吞了"""
    if not verdict or verdict.get("dominant", True):
        return ""
    return (
        "> 您这条消息里还问了别的事：上面这块先说清楚；"
        "其余问题可以到【知识库】或【智能中枢】页面继续问小购 🙂"
    )


def action_reply(verdict: dict, extra_hint_text: str = "") -> str:
    """动作意图：一次性把人工通道说清楚，不追问任何信息"""
    spec = TOPICS[verdict["topic"]]
    order_id, user_id = verdict.get("order_id") or "", verdict.get("user_id") or ""

    if order_id:
        ticket = "您这次提到的订单：**%s**，接通直接报这个号最快。" % order_id
    elif user_id:
        ticket = "您这次提到的账号：**%s**，接通报这个即可。" % user_id
    else:
        ticket = "有订单号的话报一下会更快；没有也没关系，人工可以按手机号帮您查。"

    lines = [
        "亲，先跟您说声抱歉，让您多费口舌了 🙏",
        "",
        "%s，就不问您一堆问题、也不耽误您时间了，直接走人工最快：" % spec["why"],
        "",
        "### 📞 人工售后专线：[%s](tel:%s)" % (HOTLINE, HOTLINE),
        "（%s；%s）" % (HOTLINE_HOURS, ONLINE_HOURS),
        "",
        "接通后说一句「%s」、报上订单号，人工会**一次给您办完**，不用重复描述经过。" % spec["say"],
        "",
    ]
    lines += ["- %s" % item for item in [ticket] + list(spec["facts"])]
    if extra_hint_text:
        lines += ["", extra_hint_text]
    return "\n".join(lines)


# 说明：这里**故意没有**"回答完再补一句人工电话"的函数。
# 线上反馈：用户只是问政策、问时效，回答后面却总跟一句"要办理请拨打人工"，
# 感觉在被推脱。所以人工通道只在"明确要办"时给（action_reply），咨询类正常回答就好。
def chat_reply(verdict: dict) -> str:
    """基础对话页的动作类话术：没有数据权限，只给"找谁办 + 去哪看"""
    spec = TOPICS[verdict["topic"]]
    order_id = verdict.get("order_id") or ""
    tail = ("您这次提到的订单 **%s**，接通报这个号最快。\n\n" % order_id) if order_id else ""

    text = (
        "亲，看到您说这事了，先跟您说声抱歉让您费心 🙏\n\n"
        + ("%s，我这边就不问您一堆信息了：\n\n" % spec["chat_why"])
        + ("### 📞 人工售后专线：[%s](tel:%s)\n" % (HOTLINE, HOTLINE))
        + ("（%s；%s）\n\n" % (HOTLINE_HOURS, ONLINE_HOURS))
        + ("接通直接说「%s」、报上订单号，人工一次给您办完。\n\n" % spec["say"])
        + tail
    )
    if verdict.get("kind") == "progress":
        text += "查这一单的退款进度，去「订单查询」页面看订单详情就行：\n\n[[go:agent]]"
    elif verdict["topic"] == "refund":
        text += (
            "想先看条款的话，具体政策（7 天无理由、质量问题 15 天免费退换、退款到账时效）"
            "都在知识库页面里：\n\n[[go:rag]]"
        )
    return text