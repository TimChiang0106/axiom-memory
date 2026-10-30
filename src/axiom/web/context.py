"""What every /app handler needs: the stores, settings, the sign-in check,
and the redirect-with-notice convention forms use to come back."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl, urlparse
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from axiom.auth import AppAuth
from axiom.config import Settings
from axiom.documents import DocumentStore
from axiom.store import MemoryStore
from axiom.tasks import TaskStore
from axiom.web.components import button, with_query
from axiom.web.labels import NOTICE_PARAMS


@dataclass
class WebContext:
    auth: AppAuth
    get_tasks: Callable[[], Awaitable[TaskStore]]
    get_memories: Callable[[], Awaitable[MemoryStore]]
    get_documents: Callable[[], Awaitable[DocumentStore]]
    settings: Settings
    zone: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        self.zone = ZoneInfo(self.settings.timezone)

    def local(self, at: datetime) -> str:
        return f"{at.astimezone(self.zone):%Y-%m-%d %H:%M}"

    async def guard(self, request: Request) -> Response | None:
        """None when the request may go ahead, else the response to send."""
        if not self.auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        who = await self.auth.identify(request.headers, request.cookies)
        if who is None:
            return RedirectResponse("/app/login", status_code=303)
        if request.method == "POST":
            # Same-origin forms only: the session survives cross-site
            # navigation, so a foreign page must not be able to post here.
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin request refused.", status_code=403)
        return None

    @staticmethod
    def back_to(value: str | None) -> str:
        if value and value.startswith("/app") and not value.startswith("//"):
            return value
        return "/app"

    @staticmethod
    def here(request: Request) -> str:
        """The current page, minus one-shot notices, to come back to after a form."""
        params = {k: v for k, v in request.query_params.items() if k not in NOTICE_PARAMS}
        return with_query(request.url.path, **params)

    async def notices(self, request: Request, tasks: TaskStore) -> str:
        """The error banner and the undo bar a redirect asked for."""
        html = ""
        if error := request.query_params.get("error"):
            html += f'<div class="err">{escape(error)}</div>'
        if forgot := request.query_params.get("forgot"):
            html += (
                '<div class="notice" role="status">'
                f"<span>已刪除記憶「{escape(forgot)}」</span></div>"
            )
        undo = request.query_params.get("undo", "")
        if undo.isdigit() and (task := await tasks.get(int(undo))) is not None:
            verb = {"done": "已完成", "dropped": "已刪除"}.get(task.status)
            if verb:
                html += (
                    f'<div class="notice" role="status"><span>{verb}「{escape(task.title)}」</span>'
                    f"{button(task.id, 'reopen', '復原', back=self.here(request))}</div>"
                )
        return html

    @staticmethod
    def redirect(back: str, **params: str | int) -> RedirectResponse:
        if not params:
            return RedirectResponse(back, status_code=303)
        keep = {k: v for k, v in parse_qsl(urlparse(back).query) if k not in NOTICE_PARAMS}
        return RedirectResponse(with_query(back, **keep, **params), status_code=303)
