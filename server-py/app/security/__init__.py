"""提示词安全防护模块

对外只需要：

    from app.security import guard

    verdict = await guard.check(text, user_id=..., route="chat")
    if verdict.blocked:
        ...  # verdict.message 是给用户看的话术

    answer, leaked = await guard.check_output(answer, route="chat")
    context, hits = await guard.sanitize_context(docs_text, route="rag")
"""
from app.security.guard import Verdict, guard  # noqa: F401

__all__ = ["guard", "Verdict"]
