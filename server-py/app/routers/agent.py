"""Agent 路由：ReAct Agent 订单查询（SSE）

会话缓存：历史优先从 Redis 读（key = session:{session_id}，TTL 30 分钟），
        命中后前端不必再回传全量 history。
Token 统计：整条 Agent 链路（含多轮工具调用）的 token 消耗一起统计。
"""
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from app.agents.customer_agent import create_customer_agent
from app.chains import kb_bridge
from app.db.redis_client import SESSION_TTL_SECONDS, append_turn
from app.db.session import resolve_session
from app.observability.usage import RequestUsage
from app.security import guard
from app.utils import grounding, handoff
from app.utils.messages import to_lc_messages

router = APIRouter()

_agent_app = create_customer_agent()


class ChatMessage(BaseModel):
    role: str
    content: str


class AgentRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    session_id: str | None = None
    # 前端生成的访客标识，用于管理员后台按用户查看聊天记录
    user_id: str | None = None
    user_name: str | None = None


def _send(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def agent_stream(req: AgentRequest):
    if not req.message:
        return JSONResponse(status_code=400, content={"error": "message 不能为空"})

    async def event_generator():
        session_id, history = await resolve_session(
            req.session_id, req.history, req.message
        )
        usage = RequestUsage(
            route="agent", session_id=session_id,
            user_id=req.user_id or "", user_name=req.user_name or "",
        )
        usage.set_question(req.message)
        usage.stage("input", text=req.message, chars=len(req.message),
                    history_rounds=len(history or []))
        status = "ok"
        steps: list[dict] = []
        answer = ""
        final_message = ""
        tool_facts: list[str] = []

        yield _send("session", {"session_id": session_id, "ttl": SESSION_TTL_SECONDS})

        # 提示词安全检查（白名单 → 规则 → 小模型）
        verdict = await guard.check(
            req.message, user_id=usage.user_id, user_name=usage.user_name,
            route="agent", history=history,
        )
        usage.stage("guard", **verdict.as_dict())
        if verdict.blocked:
            usage.stage("blocked", message=verdict.message)
            usage.set_answer(verdict.message)
            usage.set_error("blocked/%s/%s" % (verdict.layer, verdict.category))
            yield _send("error", {"content": verdict.message, "error": verdict.message,
                                  "blocked": True, "layer": verdict.layer})
            yield _send("usage", {"usage": await usage.finish(status="blocked")})
            yield _send("done", {"done": True})
            return

        # 退款族意图判定（规则快路径 → 小模型 → fail-open 走正常链路）
        #   action   → 这套工具只能查不能办，一次都不问，直接给人工通道
        #   info     → 咨询类：这一页没有知识库，借同一套检索链路回答
        #   其余      → 原样放行（订单工具 / 大模型）
        judge = await handoff.classify(req.message)
        plan = handoff.decide(judge, "agent")
        usage.stage("handoff", kind=judge["kind"], layer=judge["layer"],
                    topic=judge.get("topic") or "", matched=judge.get("matched") or "",
                    order_id=judge.get("order_id") or "",
                    model=judge.get("model") or "", model_tokens=judge.get("model_tokens") or 0,
                    latency_ms=judge.get("latency_ms"), action=judge.get("reason") or "")
        if plan["reply"]:
            reply = plan["reply"]
            yield _send("content", {"content": reply})
            yield _send("answer", {"content": reply})
            await append_turn(session_id, req.message, reply)
            usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
            usage.stage("answer", text=reply, chars=len(reply))
            usage.set_answer(reply)
            yield _send("usage", {"usage": await usage.finish(status="ok")})
            yield _send("done", {"done": True})
            return

        if plan["kb"]:
            # 咨询型问题（退款政策 / 时效）：借知识库检索回答，不靠大模型自由发挥
            kb_answer = ""
            try:
                prepared = await kb_bridge.prepare(req.message, config={"callbacks": usage.callbacks})
                sources = kb_bridge.sources_of(prepared)
                if sources:
                    usage.stage("sources", count=len(sources), action="推送知识库依据给前端")
                async for chunk in kb_bridge.stream_prepared(
                    prepared, req.message, config={"callbacks": usage.callbacks}
                ):
                    kb_answer += chunk
                    yield _send("content", {"content": chunk})
                if sources:
                    # 回答了知识库里的内容，就把出处标出来
                    hint = kb_bridge.source_line(sources)
                    kb_answer += hint
                    yield _send("content", {"content": hint})
                # 不再自动补人工电话：咨询类问题正常回答即可
                yield _send("answer", {"content": kb_answer})
                await append_turn(session_id, req.message, kb_answer)
                usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
                usage.stage("answer", text=kb_answer, chars=len(kb_answer))
                usage.set_answer(kb_answer)
                usage.set_steps([{"tool": "kb_bridge", "input": {"question": req.message},
                                  "obs": "借用知识库检索（本页无知识库）"}])
            except Exception as error:
                status = "error"
                usage.set_error(error)
                print(f"[Agent KB Error] {error}")
                yield _send("error", {"content": "查询出错，请重试"})
            yield _send("usage", {"usage": await usage.finish(status=status)})
            yield _send("done", {"done": True})
            return

        try:
            # 两种流模式：
            #   messages —— 模型逐 token 输出（Agent 页现在是真的边生成边显示）
            #   updates  —— 节点级事件：工具调用与工具结果，用来驱动「调用工具」步骤条
            async for mode, data in _agent_app.astream(
                {"messages": to_lc_messages(history) + [HumanMessage(content=req.message)]},
                config={"callbacks": usage.callbacks},
                stream_mode=["updates", "messages"],
            ):
                if mode == "messages":
                    chunk, _meta = data
                    if isinstance(chunk, ToolMessage):
                        continue
                    content = getattr(chunk, "content", "")
                    if isinstance(content, str) and content:
                        answer += content
                        yield _send("content", {"content": content})
                    continue

                for value in data.values():
                    messages = value.get("messages") if isinstance(value, dict) else None
                    for message in messages or []:
                        if isinstance(message, ToolMessage):
                            # 工具结果补到上一条步骤上
                            if steps:
                                steps[-1]["observation"] = message.content
                            tool_facts.append(message.content or "")
                            usage.stage("tool_result",
                                        tool=steps[-1].get("tool") if steps else "",
                                        observation=message.content, action="工具返回")
                            continue

                        tool_calls = getattr(message, "tool_calls", None)
                        if tool_calls:
                            # 进入工具调用轮：清掉上一轮的预告文字，再推送步骤
                            if answer:
                                answer = ""
                                yield _send("reset", {})
                            for call in tool_calls:
                                step = {
                                    "tool": call.get("name"),
                                    "toolInput": call.get("args"),
                                    "observation": "",
                                }
                                steps.append(step)
                                usage.stage("tool", tool=call.get("name"),
                                            toolInput=call.get("args"), action="调用工具")
                                yield _send("step", step)
                        elif getattr(message, "content", ""):
                            # 最终回答（updates 里是完整文本，作为权威结果）
                            final_message = message.content

            final_answer = final_message or answer

            # 出口接地校验：回答里的订单号必须在工具返回的事实里（或用户自己说的）找得到
            facts = grounding.facts_text(req.message, tool_facts)
            final_answer, incident = grounding.sanitize(final_answer, facts, route="agent")
            if incident:
                usage.stage("grounding_blocked", **incident)
                print(f"[Agent] 接地校验失败，已拦截编造回答：{incident.get('offending')}")

            usage.stage("answer", text=final_answer, chars=len(final_answer))
            usage.set_answer(final_answer)
            usage.set_steps(steps)
            yield _send("answer", {"content": final_answer})
            if final_answer:
                await append_turn(session_id, req.message, final_answer)
        except Exception as err:
            status = "error"
            usage.set_error(err)
            usage.set_steps(steps)
            print(f"[Agent Error] {err}")
            yield _send("error", {"content": "处理请求时出错，请重试"})

        yield _send("usage", {"usage": await usage.finish(status=status)})
        yield _send("done", {"done": True})

    return StreamingResponse(event_generator(), media_type="text/event-stream")
