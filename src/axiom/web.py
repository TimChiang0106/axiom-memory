"""The /app pages: a phone-first work home over tasks and project overviews.

The home page exists to answer one question — "is everything taken care
of?" — so it leads with a one-line status (how many today, how many already
arranged for later, how many still need a day) and then lists only what
needs a look now. Memories are shown read-only; the only writes the pages
make are task actions.

Server-rendered HTML with plain forms (POST, then redirect back): no build
step, no client state, works on any phone browser.
"""

import re
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from html import escape
from urllib.parse import quote, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from axiom.auth import AppAuth
from axiom.models import Memory, Task
from axiom.store import MemoryStore
from axiom.tasks import TaskError, TaskStore, later_this_week

WEEKDAYS = "一二三四五六日"
TRIGGER_LABELS = {
    "unrecorded_commitment": "對話中提到但沒記下的承諾",
    "due_soon": "截止日將近",
    "overview_stale": "專案總覽落後",
}
OPEN_WARN = 5

_CSS = """
:root{color-scheme:dark;--bg:#111;--card:#1b1b1b;--line:#2a2a2a;--fg:#e8e8e8;--dim:#8a8a8a;
--accent:#7fb3ff;--warn:#e0a35a;--bad:#e06c6c;--ok:#7cc48a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,
"PingFang TC","Noto Sans TC",sans-serif}
main{max-width:720px;margin:0 auto;padding:12px 14px 48px}
nav{display:flex;gap:4px;position:sticky;top:0;background:var(--bg);padding:10px 0;z-index:1}
nav a{flex:1;text-align:center;padding:8px;border-radius:8px;color:var(--dim);
text-decoration:none}
nav a.on{background:var(--card);color:var(--fg)}
a{color:var(--accent)}
h2{font-size:14px;color:var(--dim);font-weight:600;margin:24px 0 8px;letter-spacing:.04em}
.status{font-size:18px;margin:8px 0 4px}
.status .todo{color:var(--warn)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;
margin:8px 0}
.title{font-size:16px}
.meta{font-size:13px;color:var(--dim);margin-top:2px}
.flag{font-size:13px;color:var(--warn);margin-top:4px}
.bad{color:var(--bad)}
.actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.actions form{margin:0}
button{background:#262626;color:var(--fg);border:1px solid var(--line);border-radius:8px;
padding:7px 12px;font:inherit;font-size:14px}
button.primary{background:#20324d;border-color:#2c4a73}
details.more{display:inline-block}
details.more summary{list-style:none;cursor:pointer;background:#262626;border:1px solid var(--line);
border-radius:8px;padding:7px 12px;font-size:14px}
details.more summary::-webkit-details-marker{display:none}
details.more[open] .days{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
input[type=text],input[type=password]{background:#0d0d0d;color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:7px 10px;font:inherit;font-size:14px}
.reject{display:flex;gap:6px;flex:1;min-width:220px}
.reject input{flex:1}
.empty{color:var(--dim);padding:8px 0}
.week{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.day{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px;
min-height:72px}
.day.today{border-color:#2c4a73}
.day h3{font-size:13px;color:var(--dim);margin:0 0 6px;font-weight:600}
.day .t{font-size:14px;margin:4px 0}
.done{text-decoration:line-through;color:var(--dim)}
.body{white-space:pre-wrap;font-size:14px;margin-top:8px;color:#cfcfcf}
.stats{font-size:14px;margin-top:4px}
details summary{cursor:pointer;color:var(--dim);font-size:14px}
.err{background:#3a1d1d;border:1px solid #5a2a2a;border-radius:8px;padding:8px 10px;margin:8px 0}
"""


def _page(title: str, active: str, body: str) -> HTMLResponse:
    tabs = [
        ("today", "/app", "今天"),
        ("week", "/app/week", "本週"),
        ("projects", "/app/projects", "專案"),
    ]
    nav = "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
        for key, href, label in tabs
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>{escape(title)} · Axiom</title><style>{_CSS}</style></head>"
        f"<body><main><nav>{nav}</nav>{body}</main></body></html>"
    )


def _day(d: date, today: date | None = None) -> str:
    if today is not None:
        if d == today:
            return "今天"
        if d == today + timedelta(days=1):
            return "明天"
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def _meta(task: Task, today: date) -> str:
    parts = []
    if task.project:
        parts.append(escape(task.project))
    if task.planned_date:
        parts.append(f"計畫 {_day(task.planned_date, today)}")
    elif task.someday:
        parts.append("之後再說")
    if task.due_date:
        overdue = task.due_date < today
        label = f"截止 {_day(task.due_date, today)}"
        parts.append(f'<span class="bad">{label}</span>' if overdue else label)
    return f'<div class="meta">{"・".join(parts)}</div>' if parts else ""


def _button(
    task_id: int,
    action: str,
    label: str,
    *,
    back: str,
    extra: str = "",
    cls: str = "",
    confirm: str = "",
) -> str:
    onsubmit = f" onsubmit=\"return confirm('{confirm}')\"" if confirm else ""
    return (
        f'<form method="post" action="/app/tasks/{task_id}/{action}"{onsubmit}>'
        f'<input type="hidden" name="next" value="{back}">{extra}'
        f'<button class="{cls}">{label}</button></form>'
    )


def _task_card(task: Task, today: date, *, back: str = "/app") -> str:
    flags = ""
    if task.defer_count >= 3:
        flags += f'<div class="flag">延了 {task.defer_count} 次，還要做嗎？</div>'
    days = "".join(
        _button(
            task.id,
            "defer",
            _day(d),
            back=back,
            extra=f'<input type="hidden" name="to" value="{d.isoformat()}">',
        )
        for d in later_this_week(today)
    )
    actions = (
        _button(task.id, "done", "完成", back=back, cls="primary")
        + _button(
            task.id,
            "defer",
            "明天",
            back=back,
            extra='<input type="hidden" name="to" value="tomorrow">',
        )
        + '<details class="more"><summary>本週其他天</summary>'
        + f'<div class="days">{days}</div></details>'
        + _button(
            task.id,
            "defer",
            "之後再說",
            back=back,
            extra='<input type="hidden" name="to" value="someday">',
        )
        + _button(task.id, "drop", "刪除", back=back, confirm="刪除這件事？")
    )
    return (
        f'<div class="card"><div class="title">{escape(task.title)}</div>'
        f'{_meta(task, today)}{flags}<div class="actions">{actions}</div></div>'
    )


def _suggestion_card(task: Task, today: date) -> str:
    ev = task.evidence
    lines = []
    if ev and ev.trigger:
        lines.append(f"原因：{escape(TRIGGER_LABELS.get(ev.trigger, ev.trigger))}")
    if ev and ev.memories:
        links = "、".join(f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in ev.memories)
        lines.append(f"依據：{links}")
    if ev and ev.note:
        lines.append(escape(ev.note))
    if task.expires_at:
        lines.append(f"{_day(task.expires_at.date())} 前未處理會自動失效")
    evidence = "".join(f'<div class="meta">{line}</div>' for line in lines)
    reject = (
        f'<form method="post" action="/app/tasks/{task.id}/reject" class="reject">'
        '<input type="hidden" name="next" value="/app">'
        '<input type="text" name="reason" placeholder="拒絕原因（選填）">'
        "<button>拒絕</button></form>"
    )
    return (
        f'<div class="card"><div class="title">{escape(task.title)}</div>'
        f'{_meta(task, today)}{evidence}<div class="actions">'
        f"{_button(task.id, 'confirm', '確認', back='/app', cls='primary')}{reject}</div></div>"
    )


def _section(title: str, cards: list[str], *, empty: str | None = None) -> str:
    if not cards and empty is None:
        return ""
    inner = "".join(cards) if cards else f'<div class="empty">{empty}</div>'
    return f"<h2>{title}</h2>{inner}"


def _inline_md(text: str) -> str:
    """Escape, then render **bold** — enough for overview memories."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))


def build_routes(
    *,
    auth: AppAuth,
    get_tasks: Callable[[], Awaitable[TaskStore]],
    get_memories: Callable[[], Awaitable[MemoryStore]],
) -> list[Route]:
    async def guard(request: Request) -> Response | None:
        if not auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        who = await auth.identify(request.headers, request.cookies)
        if who is None:
            return RedirectResponse("/app/login", status_code=303)
        if request.method == "POST":
            # Same-origin forms only: the session survives cross-site
            # navigation, so a foreign page must not be able to post here.
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin request refused.", status_code=403)
        return None

    def back_to(value: str | None) -> str:
        if value and value.startswith("/app") and not value.startswith("//"):
            return value
        return "/app"

    async def today_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        view = await tasks.today_view()
        today = view.today
        n_today = len(view.overdue) + len(view.due_today)
        status = f"今天 {n_today} 件・其他 {view.arranged} 件都已排好日期"
        if view.unscheduled:
            status += f'<div class="todo">{len(view.unscheduled)} 件還沒安排</div>'
        error = request.query_params.get("error")
        body = f'<div class="status">{status}</div>'
        if error:
            body += f'<div class="err">{escape(error)}</div>'
        body += _section("逾期", [_task_card(t, today) for t in view.overdue])
        body += _section("今天", [_task_card(t, today) for t in view.due_today])
        body += _section(
            "還沒安排",
            [_task_card(t, today) for t in view.unscheduled],
            empty="全部都有安排了" if (view.overdue or view.due_today or view.suggested) else None,
        )
        body += _section("AI 建議待確認", [_suggestion_card(t, today) for t in view.suggested])
        if not (view.overdue or view.due_today or view.unscheduled or view.suggested):
            body += '<div class="empty">今天沒有要處理的事。</div>'
        return _page("今天", "today", body)

    async def week_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        today = await tasks.today()
        view = await tasks.week_view(today)
        cols = []
        for i, day_tasks in enumerate(view.days):
            d = view.monday + timedelta(days=i)
            items = (
                "".join(
                    f'<div class="t{" done" if t.status == "done" else ""}">{escape(t.title)}</div>'
                    for t in day_tasks
                )
                or '<div class="t empty">—</div>'
            )
            cls = "day today" if d == today else "day"
            cols.append(
                f'<div class="{cls}"><h3>週{WEEKDAYS[i]} {d.month}/{d.day}</h3>{items}</div>'
            )
        body = f"<h2>{_day(view.monday)} – {_day(view.monday + timedelta(days=6))}</h2>"
        body += f'<div class="week">{"".join(cols)}</div>'
        groups: dict[str, list[Task]] = {}
        for t in view.done:
            groups.setdefault(t.project or "未分專案", []).append(t)
        body += f"<h2>本週已完成（{len(view.done)}）</h2>"
        if not groups:
            body += '<div class="empty">還沒有。</div>'
        for project, items in groups.items():
            lis = "".join(f'<div class="meta">{escape(t.title)}</div>' for t in items)
            body += (
                f'<div class="card"><div class="title">{escape(project)}（{len(items)}）</div>'
                f"{lis}</div>"
            )
        return _page("本週", "week", body)

    async def projects_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        tasks = await get_tasks()
        memories = await get_memories()
        today = await tasks.today()
        stats = await tasks.project_stats()
        overviews = await memories.overviews()
        drifted = {d.name: d for d in await memories.drifted_overviews()}
        orphans = await memories.orphans()

        def card(project: str | None, overview: Memory | None) -> str:
            s = stats.get(project)
            name = project or "未分專案"
            line = (
                f"open {s.open if s else 0}・本週完成 {s.done_this_week if s else 0}"
                f"・總完成 {s.done_total if s else 0}"
            )
            if s and s.next_due:
                line += f"・最近截止 {_day(s.next_due, today)}"
            flags = ""
            if s and s.open > OPEN_WARN:
                flags += f'<div class="flag">open {s.open} 筆，超過 {OPEN_WARN} 筆，該收斂了</div>'
            if overview and overview.name in drifted:
                newer = "、".join(drifted[overview.name].newer_related)
                flags += f'<div class="flag">總覽落後於：{escape(newer)}</div>'
            mine = [o for o in orphans if project and o.name.startswith(f"{project}-")]
            if mine:
                names = "、".join(
                    f'<a href="/app/memory/{escape(o.name)}">{escape(o.name)}</a>' for o in mine
                )
                flags += (
                    f'<details><summary class="flag">{len(mine)} 筆記憶沒連到總覽</summary>'
                    f'<div class="meta">{names}</div></details>'
                )
            if s and s.stale_open:
                for t in s.stale_open:
                    flags += (
                        f'<div class="flag">「{escape(t.title)}」{tasks.stale_open_days} '
                        "天沒動，還要做嗎？</div>"
                    )
            body = ""
            if overview:
                body = (
                    f"<details><summary>總覽（{overview.updated_at:%Y-%m-%d} 更新）</summary>"
                    f'<div class="body">{_inline_md(overview.content)}</div></details>'
                )
            return (
                f'<div class="card"><div class="title">{escape(name)}</div>'
                f'<div class="stats">{line}</div>{flags}{body}</div>'
            )

        known = {o.name.removesuffix("-overview") for o in overviews}
        # Busiest projects first, so the page leads with where work is.
        ordered = sorted(
            overviews,
            key=lambda o: (
                -(stats[p].open if (p := o.name.removesuffix("-overview")) in stats else 0)
            ),
        )
        cards = [card(o.name.removesuffix("-overview"), o) for o in ordered]
        cards += [card(p, None) for p in stats if p not in known]
        return _page("專案", "projects", "".join(cards) or '<div class="empty">沒有專案。</div>')

    async def memory_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        memories = await get_memories()
        memory = await memories.get(request.path_params["name"])
        if memory is None:
            return _page("找不到", "", '<div class="empty">找不到這筆記憶。</div>')
        related = "、".join(
            f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in memory.related
        )
        body = (
            f'<div class="card"><div class="title">{escape(memory.name)}</div>'
            f'<div class="meta">{escape(memory.description)}</div>'
            f'<div class="meta">{memory.type}・{memory.category}・'
            f"{memory.updated_at:%Y-%m-%d} 更新</div>"
            f'<div class="body">{_inline_md(memory.content)}</div>'
            + (f'<div class="meta">相關：{related}</div>' if related else "")
            + "</div>"
        )
        return _page(memory.name, "", body)

    async def task_action(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        form = await request.form()
        back = back_to(str(form.get("next") or ""))
        tasks = await get_tasks()
        task_id = int(request.path_params["task_id"])
        action = request.path_params["action"]
        try:
            if action == "done":
                await tasks.done(task_id)
            elif action == "drop":
                await tasks.drop(task_id)
            elif action == "confirm":
                await tasks.confirm(task_id)
            elif action == "reject":
                await tasks.reject(task_id, reason=str(form.get("reason") or ""))
            elif action == "defer":
                to = str(form.get("to") or "")
                await tasks.defer(
                    task_id,
                    to=to if to in ("tomorrow", "someday") else date.fromisoformat(to),  # type: ignore[arg-type]
                )
            else:
                return HTMLResponse("Unknown action.", status_code=404)
        except (TaskError, ValueError) as e:
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=303)
        return RedirectResponse(back, status_code=303)

    async def login_page(request: Request) -> Response:
        if not auth.configured:
            return HTMLResponse("App auth is not configured.", status_code=503)
        error = ""
        if request.method == "POST":
            form = await request.form()
            if auth.check_token(str(form.get("token") or "")):
                response = RedirectResponse("/app", status_code=303)
                response.set_cookie(
                    AppAuth.COOKIE,
                    auth.session_value(),
                    max_age=60 * 60 * 24 * 30,
                    path="/app",
                    httponly=True,
                    secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
                    samesite="strict",
                )
                return response
            error = '<div class="err">不對。</div>'
        elif await auth.identify(request.headers, request.cookies):
            return RedirectResponse("/app", status_code=303)
        body = (
            f"<h2>登入</h2>{error}"
            '<form method="post" class="actions"><input type="password" name="token" '
            'placeholder="Service token" autocomplete="current-password">'
            '<button class="primary">進入</button></form>'
        )
        return _page("登入", "", body)

    return [
        Route("/app", today_page, methods=["GET"]),
        Route("/app/week", week_page, methods=["GET"]),
        Route("/app/projects", projects_page, methods=["GET"]),
        Route("/app/memory/{name}", memory_page, methods=["GET"]),
        Route("/app/tasks/{task_id:int}/{action}", task_action, methods=["POST"]),
        Route("/app/login", login_page, methods=["GET", "POST"]),
    ]
