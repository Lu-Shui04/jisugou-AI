"""
第一章：FastAPI 路由
GET  /api/chat/health  - 健康检查（含 Redis 状态）
POST /api/chat         - 普通对话（一次性返回，带会话缓存 + Token 统计）
POST /api/chat/stream  - 流式对话（SSE，带会话缓存 + Token 统计）

「基础对话」是纯 Chain：只有 Prompt + 记忆，没有工具、没有数据权限。
它的职责是聊天和指路 —— 回答里可以带 [[go:agent]] / [[go:rag]] / [[go:graph]]
标记，前端会渲染成跳转到「订单查询 / 知识库 / 智能中枢」的按钮。
需要真实数据的场景由那三个页面负责。

会话缓存：请求体传 session_id 即命中服务端会话（key = session:{session_id}，TTL 30 分钟），
        不传则自动生成并通过 session 事件返回给前端。
"""
import json
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.chains import kb_bridge
from app.chains.basic_chat import (
    customer_service_chain,
    customer_service_stream_chain,
    format_history,
)
from app.db.redis_client import SESSION_TTL_SECONDS, append_turn, ping as redis_ping
from app.db.session import resolve_session
from app.observability.usage import RequestUsage
from app.security import guard
from app.utils import grounding, handoff

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    session_id: str | None = None
    # 前端生成的访客标识，用于管理员后台按用户查看聊天记录
    user_id: str | None = None
    user_name: str | None = None


def _now():
    return datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def _send(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


# ─── 健康检查 ────────────────────────────────────────────────────
@router.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "redis": await redis_ping(),
        "session_ttl_seconds": SESSION_TTL_SECONDS,
        "mode": "basic-chain（只聊天与引导，无工具）",
    }


# ─── 普通对话接口 ────────────────────────────────────────────────
@router.post("")
async def chat(req: ChatRequest):
    if not req.message:
        return JSONResponse(status_code=400, content={"error": "message 字段不能为空"})

    session_id, history = await resolve_session(
        req.session_id, req.history, req.message
    )
    usage = RequestUsage(
        route="chat", session_id=session_id,
        user_id=req.user_id or "", user_name=req.user_name or "",
    )
    usage.set_question(req.message)
    usage.stage("input", text=req.message, chars=len(req.message),
                history_rounds=len(history or []))

    # 提示词安全检查（白名单 → 规则 → 小模型）
    verdict = await guard.check(
        req.message, user_id=usage.user_id, user_name=usage.user_name,
        route="chat", history=history,
    )
    usage.stage("guard", **verdict.as_dict())
    if verdict.blocked:
        usage.stage("blocked", message=verdict.message)
        usage.set_answer(verdict.message)
        usage.set_error("blocked/%s/%s" % (verdict.layer, verdict.category))
        await usage.finish(status="blocked")
        return JSONResponse(
            status_code=200,
            content={"content": verdict.message, "blocked": True,
                     "reason": verdict.reason, "layer": verdict.layer},
        )

    # 退款 / 退货 / 改地址 / 投诉：基础对话没有办理权限，一次都不追问，直接给人工通道
    judge = await handoff.classify(req.message)
    plan = handoff.decide(judge, "chat")
    usage.stage("handoff", kind=judge["kind"], layer=judge["layer"],
                    topic=judge.get("topic") or "", matched=judge.get("matched") or "",
                    order_id=judge.get("order_id") or "",
                    model=judge.get("model") or "", model_tokens=judge.get("model_tokens") or 0,
                    latency_ms=judge.get("latency_ms"), action=judge.get("reason") or "")
    if plan["reply"]:
        reply = plan["reply"]
        await append_turn(session_id, req.message, reply)
        usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
        usage.set_answer(reply)
        return {
            "content": reply,
            "session_id": session_id,
            "handoff": judge,
            "usage": await usage.finish(),
        }

    if plan["kb"]:
        # 咨询型问题（政策 / 时效）：这一页没有知识库，借同一套检索链路回答
        reply = ""
        async for chunk in kb_bridge.stream(req.message, config={"callbacks": usage.callbacks}):
            reply += chunk
        # 不再自动补人工电话：只有"明确要办"的动作类才会给人工通道
        await append_turn(session_id, req.message, reply)
        usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
        usage.set_answer(reply)
        return {
            "content": reply,
            "session_id": session_id,
            "handoff": judge,
            "usage": await usage.finish(),
        }

    try:
        response = await customer_service_chain.ainvoke(
            {
                "user_input": req.message,
                "chat_history": format_history(history),
                "current_time": _now(),
            },
            config={"callbacks": usage.callbacks},
        )
        # 输出侧检查：防止系统提示词被套出来
        response, _leaked = await guard.check_output(
            response, route="chat", user_id=usage.user_id
        )
        usage.stage("output", leaked=_leaked)
        # 接地校验：基础对话没有数据权限，回答里出现的订单号只能来自用户自己说的
        response, grounding_incident = grounding.sanitize(
            response, grounding.facts_text(req.message, history), route="chat"
        )
        usage.stage("grounding", blocked=bool(grounding_incident),
                    **(grounding_incident or {}))
        # 写入会话缓存并续期 TTL
        await append_turn(session_id, req.message, response)
        usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
        usage.stage("answer", text=response, chars=len(response))
        usage.set_answer(response)
        return {
            "content": response,
            "session_id": session_id,
            "usage": await usage.finish(),
        }
    except Exception as error:
        usage.set_error(error)
        await usage.finish(status="error")
        print(f"[Chat Error] {error}")
        return JSONResponse(status_code=500, content={"error": "服务暂时不可用，请稍后重试"})


# ─── 流式对话接口（SSE）─────────────────────────────────────────
@router.post("/stream")
async def chat_stream(req: ChatRequest):
    if not req.message:
        return JSONResponse(status_code=400, content={"error": "message 字段不能为空"})

    async def event_generator():
        session_id, history = await resolve_session(
            req.session_id, req.history, req.message
        )
        usage = RequestUsage(
            route="chat", session_id=session_id,
            user_id=req.user_id or "", user_name=req.user_name or "",
        )
        usage.set_question(req.message)
        usage.stage("input", text=req.message, chars=len(req.message),
                    history_rounds=len(history or []))
        answer = ""
        status = "ok"

        yield _send("session", {"session_id": session_id, "ttl": SESSION_TTL_SECONDS})

        # 提示词安全检查：命中直接给安全话术，不再调用大模型
        verdict = await guard.check(
            req.message, user_id=usage.user_id, user_name=usage.user_name,
            route="chat", history=history,
        )
        usage.stage("guard", **verdict.as_dict())
        if verdict.blocked:
            usage.stage("blocked", message=verdict.message)
            usage.set_answer(verdict.message)
            usage.set_error("blocked/%s/%s" % (verdict.layer, verdict.category))
            yield _send("content", {"content": verdict.message})
            yield _send("blocked", {"blocked": True, "content": verdict.message,
                                    "error": verdict.message, "reason": verdict.reason,
                                    "layer": verdict.layer, "category": verdict.category})
            yield _send("usage", {"usage": await usage.finish(status="blocked")})
            yield _send("done", {"done": True})
            return

        # 退款 / 退货 / 改地址 / 投诉：直接在人工通道这里结束，不问订单号、不编流程
        judge = await handoff.classify(req.message)
        plan = handoff.decide(judge, "chat")
        usage.stage("handoff", kind=judge["kind"], layer=judge["layer"],
                    topic=judge.get("topic") or "", matched=judge.get("matched") or "",
                    order_id=judge.get("order_id") or "",
                    model=judge.get("model") or "", model_tokens=judge.get("model_tokens") or 0,
                    latency_ms=judge.get("latency_ms"), action=judge.get("reason") or "")
        if plan["reply"]:
            reply = plan["reply"]
            yield _send("content", {"content": reply})
            await append_turn(session_id, req.message, reply)
            usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
            usage.stage("answer", text=reply, chars=len(reply))
            usage.set_answer(reply)
            yield _send("usage", {"usage": await usage.finish(status="ok")})
            yield _send("done", {"done": True})
            return

        if plan["kb"]:
            # 只有"退款/退货政策、时效"这类咨询才借知识库回答（这是修"用转人工回答一个有标准答案的问题"
            # 那条线上反馈）。**其它问题一律回到原来的"聊天 + 指路"**：基础对话的职责就是聊天和指路，
            # 不承担知识库问答（产品参数、价格请走知识库页 / 智能中枢）。
            kb_answer = ""
            try:
                prepared = await kb_bridge.prepare(
                    req.message, config={"callbacks": usage.callbacks}
                )
                sources = kb_bridge.sources_of(prepared)
                if sources:
                    # 命中就以知识库为准（顺带把来源推给前端：知识库页可以点开看原文）
                    yield _send("sources", {"sources": sources})
                    usage.stage("sources", count=len(sources), action="推送引用来源给前端")

                async for chunk in kb_bridge.stream_prepared(
                    prepared, req.message, config={"callbacks": usage.callbacks}
                ):
                    kb_answer += chunk
                    yield _send("content", {"content": chunk})

                # 来源用前端的「📎 依据」标签展示（sources 事件），正文里不再重复一遍；
                # 也不再自动补人工电话（用户只是问，正常回答就好）
                if plan["kb"] and not sources:
                    # 想办退款这类但没有命中知识库：给一个去知识库看条款的入口
                    guide = "\n\n想先看条款的话，具体政策都在知识库页面里：\n\n[[go:rag]]"
                    kb_answer += guide
                    yield _send("content", {"content": guide})

                await append_turn(session_id, req.message, kb_answer)
                usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
                usage.stage("answer", text=kb_answer, chars=len(kb_answer))
                usage.set_answer(kb_answer)
            except Exception as error:
                status = "error"
                usage.set_error(error)
                print(f"[Chat KB Error] {error}")
                yield _send("error", {"error": "查询出错，请重试"})
            yield _send("usage", {"usage": await usage.finish(status=status)})
            yield _send("done", {"done": True})
            return

        try:
            async for chunk in customer_service_stream_chain.astream(
                {
                    "user_input": req.message,
                    "chat_history": format_history(history),
                    "current_time": _now(),
                },
                config={"callbacks": usage.callbacks},
            ):
                if chunk:
                    answer += chunk
                    yield _send("content", {"content": chunk})
        except Exception as error:
            status = "error"
            usage.set_error(error)
            print(f"[Stream Error] {error}")
            yield _send("error", {"error": "生成回复时出错，请重试"})

        # 输出侧检查：万一模型把系统提示词复述出来，替换成安全话术
        answer, leaked = await guard.check_output(answer, route="chat", user_id=usage.user_id)
        usage.stage("output", leaked=leaked)
        if leaked:
            yield _send("content", {"content": "\n\n" + answer})

        # 接地校验：基础对话没有数据权限，回答里的订单号只能来自用户自己说过的
        answer, grounding_incident = grounding.sanitize(
            answer, grounding.facts_text(req.message, history), route="chat"
        )
        usage.stage("grounding", blocked=bool(grounding_incident),
                    **(grounding_incident or {}))
        if grounding_incident:
            yield _send("content", {"content": answer})

        # 会话落缓存（续期 30 分钟）
        if answer:
            await append_turn(session_id, req.message, answer)
            usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
        usage.stage("answer", text=answer, chars=len(answer))
        usage.set_answer(answer)

        # 记录本次请求的 Token 消耗
        yield _send("usage", {"usage": await usage.finish(status=status)})
        yield _send("done", {"done": True})

    return StreamingResponse(event_generator(), media_type="text/event-stream")
