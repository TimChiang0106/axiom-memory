"""The /app routes: task pages, memory pages, sign-in and static files."""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from axiom.auth import AppAuth
from axiom.config import Settings, get_settings
from axiom.documents import DocumentStore
from axiom.store import MemoryStore
from axiom.tasks import TaskStore
from axiom.web import memory_pages, task_pages
from axiom.web.context import WebContext
from axiom.web.layout import asset_routes, page


def build_routes(
    *,
    auth: AppAuth,
    get_tasks: Callable[[], Awaitable[TaskStore]],
    get_memories: Callable[[], Awaitable[MemoryStore]],
    get_documents: Callable[[], Awaitable[DocumentStore]],
    settings: Settings | None = None,
) -> list[Route]:
    ctx = WebContext(
        auth=auth,
        get_tasks=get_tasks,
        get_memories=get_memories,
        get_documents=get_documents,
        settings=settings or get_settings(),
    )

    async def login_page(request: Request) -> Response:
        if not ctx.auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        error = ""
        if request.method == "POST":
            form = await request.form()
            if ctx.auth.check_token(str(form.get("token") or "")):
                response = RedirectResponse("/app", status_code=303)
                response.set_cookie(
                    AppAuth.COOKIE,
                    ctx.auth.session_value(),
                    max_age=60 * 60 * 24 * 30,
                    path="/app",
                    httponly=True,
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
                    samesite="strict",
                )
                return response
            error = '<div class="err">不對。</div>'
        elif await ctx.auth.identify(request.headers, request.cookies):
            return RedirectResponse("/app", status_code=303)
        body = (
            f'<div class="login"><h1>登入 Axiom</h1>'
            '<div class="meta">輸入 service token，這台裝置會記住 30 天。</div>'
            f'{error}<form method="post"><input type="password" name="token" '
            'placeholder="Service token" aria-label="Service token" '
            'autocomplete="current-password">'
            '<button class="primary">進入</button></form></div>'
        )
        return page("登入", "", body)

    return [
        *task_pages.routes(ctx),
        *memory_pages.routes(ctx),
        Route("/app/login", login_page, methods=["GET", "POST"]),
        *asset_routes(),
    ]
