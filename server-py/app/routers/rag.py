"""RAG 路由：Top-K 召回 + 相似度阈值过滤（SSE）

score_threshold 可在请求里按次覆盖；不传则用 .env 的 RAG_SCORE_THRESHOLD。
阈值过滤后没有命中片段时返回兜底话术，sources 为空。

回答里的 [1][2] 编号对应 sources 里的 index，前端据此展示引用；
sources 里带完整片段内容与原始文件名，可以「查看片段」和「查看原文」核对真实来源。

GET /api/rag/sources  知识库文件清单（含章节数、片段数）
GET /api/rag/source   某个知识库文件的原文与章节（用于查看真实来源）
"""
import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.retrieval.rag_chain import KNOWLEDGE_DIR, rag_chain_with_sources, stream_answer
from app.observability.usage import RequestUsage
from app.security import guard, identity
from app.security.gate import require_gate
from app.utils import handoff

router = APIRouter()


def _knowledge_files() -> list[str]:
    """知识库目录下的 markdown 文件"""
    try:
        return sorted(
            name for name in os.listdir(KNOWLEDGE_DIR)
            if name.endswith(".md") and os.path.isfile(os.path.join(KNOWLEDGE_DIR, name))
        )
    except OSError:
        return []


def _safe_knowledge_path(name: str) -> str:
    """只允许读取知识库目录下的 md 文件，防止路径穿越"""
    file_name = os.path.basename((name or "").strip())
    if not file_name.endswith(".md"):
        return ""
    path = os.path.normpath(os.path.join(KNOWLEDGE_DIR, file_name))
    if not path.startswith(KNOWLEDGE_DIR) or not os.path.isfile(path):
        return ""
    return path


def _split_sections(content: str) -> list[dict]:
    """按 ## 标题切章节，与入库脚本一致，前端可据此跳转到命中章节"""
    sections = []
    title = "概述"
    buffer: list[str] = []

    for line in content.splitlines():
        if line.startswith("## "):
            sections.append({"title": title, "content": "\n".join(buffer).strip()})
            title = line[3:].strip()
            buffer = [line]
        else:
            buffer.append(line)
    sections.append({"title": title, "content": "\n".join(buffer).strip()})
    return [item for item in sections if item["content"].strip()]


@router.get("/sources")
async def knowledge_sources():
    """知识库文件清单"""
    files = []
    for name in _knowledge_files():
        path = _safe_knowledge_path(name)
        if not path:
            continue
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        sections = _split_sections(content)
        files.append({
            "file": name,
            "path": os.path.relpath(path, os.path.dirname(KNOWLEDGE_DIR)),
            "sections": len(sections),
            "chars": len(content),
        })
    return {"total": len(files), "files": files}


@router.get("/source")
async def knowledge_source(
    name: str = Query(..., description="知识库文件名，如 products.md"),
    section: str = Query(default="", description="要高亮的章节标题，可省略"),
):
    """知识库原文（真实来源），按 ## 切成章节返回"""
    path = _safe_knowledge_path(name)
    if not path:
        return JSONResponse(status_code=404, content={"error": "文件不存在"})

    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()

    sections = _split_sections(content)
    target = (section or "").strip()
    current = next((item for item in sections if item["title"] == target), None)

    return {
        "file": os.path.basename(path),
        "path": os.path.relpath(path, os.path.dirname(KNOWLEDGE_DIR)),
        "content": content,
        "sections": [{"title": item["title"], "chars": len(item["content"])} for item in sections],
        "matched": {
            "title": current["title"],
            "content": current["content"],
        } if current else None,
    }


class RagRequest(BaseModel):
    question: str
    session_id: str | None = None
    top_k: int | None = None
    score_threshold: float | None = None
    # 前端生成的访客标识，用于管理员后台按用户查看聊天记录
    user_id: str | None = None
    user_name: str | None = None


def _send(event_type, data):
    return f"data: {json.dumps({'type': event_type, **data}, ensure_ascii=False)}\n\n"


@router.post("/query", dependencies=[Depends(require_gate)])
async def rag_query(req: RagRequest):
    if not req.question:
        return JSONResponse(status_code=400, content={"error": "question 不能为空"})

    async def event_generator():
        # 知识库是公共数据，匿名也能查；但身份同样只认令牌（请求体里的 user_id 只当昵称）
        principal = identity.current_principal()
        identity.audit_claim(principal, req.user_id, route="rag")
        session_id = (req.session_id or "").strip() or uuid.uuid4().hex
        usage = RequestUsage(
            route="rag", session_id=session_id,
            user_id=principal.user_id, user_name=principal.user_name,
        )
        usage.stage("identity", **principal.as_dict(), claimed=req.user_id or "")
        usage.set_question(req.question)
        usage.stage("input", text=req.question, chars=len(req.question))
        status = "ok"

        yield _send("session", {"session_id": session_id})

        # 提示词安全检查（问句里同样可能藏注入）
        verdict = await guard.check(
            req.question, user_id=usage.user_id, user_name=usage.user_name, route="rag",
        )
        usage.stage("guard", **verdict.as_dict())
        if verdict.blocked:
            usage.stage("blocked", message=verdict.message)
            usage.set_answer(verdict.message)
            usage.set_error("blocked/%s/%s" % (verdict.layer, verdict.category))
            yield _send("content", {"content": verdict.message})
            yield _send("error", {"content": verdict.message, "error": verdict.message,
                                  "blocked": True, "layer": verdict.layer})
            yield _send("usage", {"usage": await usage.finish(status="blocked")})
            yield _send("done", {"done": True})
            return

        # 退款 / 退货 / 改地址 / 投诉：动作意图直接给人工通道（知识库只能查政策，办不了事）
        # 政策 / 进度类问题照常检索，由下面的 footer 补上人工入口
        judge = await handoff.classify(req.question)
        plan = handoff.decide(judge, "rag")
        usage.stage("handoff", kind=judge["kind"], layer=judge["layer"],
                    topic=judge.get("topic") or "", matched=judge.get("matched") or "",
                    order_id=judge.get("order_id") or "",
                    model=judge.get("model") or "", model_tokens=judge.get("model_tokens") or 0,
                    latency_ms=judge.get("latency_ms"), action=judge.get("reason") or "")
        reply = plan["reply"]
        if reply:
            yield _send("content", {"content": reply})
            yield _send("answer", {"content": reply, "filtered": False})
            usage.stage("answer", text=reply, chars=len(reply))
            usage.set_answer(reply)
            yield _send("usage", {"usage": await usage.finish(status="ok")})
            yield _send("done", {"done": True})
            return

        answer = ""
        try:
            # 1) 检索是同步阻塞调用，放线程池避免卡住事件循环
            prepared = await asyncio.to_thread(
                rag_chain_with_sources.prepare,
                {
                    "question": req.question,
                    "top_k": req.top_k,
                    "score_threshold": req.score_threshold,
                },
                config={"callbacks": usage.callbacks},
            )

            usage.stage("retrieval",
                        query=prepared.get("query") or req.question,
                        kept=len(prepared.get("sources") or []),
                        threshold=prepared.get("threshold"),
                        filtered=prepared.get("filtered"),
                        degraded=prepared.get("degraded"),
                        sources=[item.get("source") for item in (prepared.get("sources") or [])],
                        action="向量检索 + 阈值重排")

            # 2) 来源先推给前端，用户马上能看到引用
            if prepared.get("sources"):
                usage.stage("sources", count=len(prepared["sources"]),
                            action="推送引用来源给前端")
                yield _send("sources", {"sources": prepared["sources"]})

            if prepared.get("answer"):
                # 检索失败 / 没命中：直接回兜底话术，不用调模型
                answer = prepared["answer"]
                yield _send("content", {"content": answer})
            else:
                # 3) 逐 token 生成回答
                async for chunk in stream_answer(
                    prepared["docs"],
                    req.question,
                    config={"callbacks": usage.callbacks},
                ):
                    answer += chunk
                    yield _send("content", {"content": chunk})

            # 咨询类问题（政策 / 时效）**不再自动补人工电话**：用户只是问，正常回答就好，
            # 该找人工的时候（明确要办）走的是人工接力那条路径，不用每句都推人工

            usage.stage("answer", text=answer, chars=len(answer))
            usage.set_answer(answer)
            yield _send(
                "answer",
                {"content": answer, "filtered": prepared.get("filtered", False)},
            )
        except Exception as err:
            status = "error"
            usage.set_error(err)
            print(f"[RAG Error] {err}")
            yield _send("error", {"content": "查询出错，请重试"})

        yield _send("usage", {"usage": await usage.finish(status=status)})
        yield _send("done", {"done": True})

    return StreamingResponse(event_generator(), media_type="text/event-stream")
