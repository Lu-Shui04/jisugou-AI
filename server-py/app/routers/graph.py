"""LangGraph 工作流路由（SSE）

会话缓存：历史优先从 Redis 读（key = session:{session_id}，TTL 30 分钟）。
Token 统计：把 callbacks 挂在 graph.astream 上，各节点内部的 LLM 调用统一累计。
"""
import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import BaseModel

from app.graphs.customer_graph import build_customer_graph
from app.db.redis_client import SESSION_TTL_SECONDS, append_turn
from app.db.session import resolve_session
from app.observability.usage import RequestUsage
from app.security import guard
from app.utils import handoff
from app.utils.messages import to_lc_messages

router = APIRouter()

_graph = None

# 只有这两个节点产出最终答案，其余节点的 token 不外泄
STREAM_NODES = {"answerSynthesizer", "generalChat"}


def _get_graph():
    global _graph
    if _graph is None:
        _graph = build_customer_graph()
    return _graph


class ChatMessage(BaseModel):
    role: str
    content: str


class GraphRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    session_id: str | None = None
    # 前端生成的访客标识，用于管理员后台按用户查看聊天记录
    user_id: str | None = None
    user_name: str | None = None


def _send(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


@router.post("/stream")
async def graph_stream(req: GraphRequest):
    if not req.message:
        return JSONResponse(status_code=400, content={"error": "message 不能为空"})

    async def event_generator():
        session_id, history = await resolve_session(
            req.session_id, req.history, req.message
        )
        usage = RequestUsage(
            route="graph", session_id=session_id,
            user_id=req.user_id or "", user_name=req.user_name or "",
        )
        usage.set_question(req.message)
        usage.stage("input", text=req.message, chars=len(req.message),
                    history_rounds=len(history or []))
        status = "ok"
        final_answer = ""
        streamed_answer = ""
        # 走过的节点 + 意图，作为本次请求的“执行轨迹”记录到后台
        nodes: list[dict] = []

        yield _send("session", {"session_id": session_id, "ttl": SESSION_TTL_SECONDS})

        # 提示词安全检查：命中就不进图，省掉整条链路的 token
        verdict = await guard.check(
            req.message, user_id=usage.user_id, user_name=usage.user_name,
            route="graph", history=history,
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

        # 退款 / 退货 / 改地址 / 投诉：不进图、不调意图识别，直接给人工通道（零 token）
        judge = await handoff.classify(req.message)
        plan = handoff.decide(judge, "graph")
        usage.stage("handoff", kind=judge["kind"], layer=judge["layer"],
                    topic=judge.get("topic") or "", matched=judge.get("matched") or "",
                    order_id=judge.get("order_id") or "",
                    model=judge.get("model") or "", model_tokens=judge.get("model_tokens") or 0,
                    latency_ms=judge.get("latency_ms"), action=judge.get("reason") or "")
        reply = plan["reply"]
        if reply:
            yield _send("content", {"content": reply})
            yield _send("answer", {"content": reply})
            await append_turn(session_id, req.message, reply)
            usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
            usage.stage("answer", text=reply, chars=len(reply))
            usage.set_answer(reply)
            usage.set_steps([{"node": "handoff", "intent": judge.get("topic") or judge["kind"]}])
            yield _send("usage", {"usage": await usage.finish(status="ok")})
            yield _send("done", {"done": True})
            return

        try:
            graph = _get_graph()

            # 两种流模式一起用：
            #   updates  —— 节点级别的状态（意图、工具步骤、最终答案）
            #   messages —— 模型逐 token 的输出，只转发「最终答案节点」，避免把
            #               意图识别、订单 Agent、知识库节点的中间输出播给用户
            async for mode, data in graph.astream(
                {
                    "user_input": req.message,
                    "messages": to_lc_messages(history) + [HumanMessage(content=req.message)],
                    # 判定为"咨询"→ 直接去知识库；"问进度"→ 直接去订单工具。
                    # 预置意图能让意图识别节点跳过那一次多余的模型调用（也更准）。
                    **({"intents": plan["preset_intents"]} if plan.get("preset_intents") else {}),
                },
                config={"callbacks": usage.callbacks},
                stream_mode=["updates", "messages"],
            ):
                if mode == "messages":
                    chunk, meta = data
                    node = (meta or {}).get("langgraph_node")
                    if node not in STREAM_NODES or isinstance(chunk, ToolMessage):
                        continue
                    content = getattr(chunk, "content", "")
                    if isinstance(content, str) and content:
                        streamed_answer += content
                        yield _send("content", {"content": content})
                    continue

                node_name, node_state = next(iter(data.items()))

                intents = node_state.get("intents") or (
                    [node_state["intent"]] if node_state.get("intent") else []
                )
                nodes.append({"node": node_name, "intent": node_state.get("intent") or "",
                              "intents": intents})
                if node_name == "intentRouter":
                    usage.stage("intent", intents=intents, action="意图识别")
                else:
                    usage.stage("node", node=node_name, action="节点执行完成")
                yield _send("node", {
                    "node": node_name,
                    "intent": node_state.get("intent"),
                    "intents": intents,
                })

                # 知识库命中的来源：推给前端展示"依据"（回答了知识库内容就要标出处）
                rag_sources = node_state.get("rag_sources")
                if rag_sources:
                    usage.stage("sources", count=len(rag_sources),
                                action="推送知识库依据给前端")
                    yield _send("sources", {"sources": rag_sources})

                order_result = node_state.get("order_result")
                if order_result and order_result.get("steps"):
                    for step in order_result["steps"]:
                        usage.stage("tool", tool=step.get("tool"),
                                    toolInput=step.get("input"),
                                    observation=step.get("obs"), action="订单工具调用")
                    yield _send("steps", {"steps": order_result["steps"]})

                if node_state.get("final_answer"):
                    final_answer = node_state["final_answer"]
                    yield _send("answer", {"content": final_answer})

            # updates 没拿到答案（例如只走了闲聊）时，用流式拼出来的兜底
            final_answer = final_answer or streamed_answer
            if final_answer:
                await append_turn(session_id, req.message, final_answer)
                usage.stage("session", session_id=session_id, action="写入会话缓存并续期")
            usage.stage("answer", text=final_answer, chars=len(final_answer))
            usage.set_answer(final_answer)
            usage.set_steps(nodes)
        except Exception as err:
            status = "error"
            usage.set_error(err)
            usage.set_steps(nodes)
            print(f"[Graph Error] {err}")
            yield _send("error", {"content": "处理请求时出错，请重试"})

        yield _send("usage", {"usage": await usage.finish(status=status)})
        yield _send("done", {"done": True})

    return StreamingResponse(event_generator(), media_type="text/event-stream")
