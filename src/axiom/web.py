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
:root{color-scheme:light;
--bg:#FFFFFF;--surface:#FFFFFF;--line:#E3E5EC;--fg:#1B1F2A;--dim:#5B6173;--input:#7E8494;
--primary:#1B1F2A;--primary-hover:#363A44;--on-primary:#FFFFFF;--selected:#EDEDEE;
--ok:#1E7F4F;--warn:#9A5B00;--bad:#C62828;--info:#2F3A8F;
--r-tag:4px;--r-ctl:12px;--r-card:20px}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-size:16px;line-height:1.6;
font-family:"DM Sans","Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;
font-variant-numeric:tabular-nums}
header{position:sticky;top:0;z-index:1;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{max-width:720px;margin:0 auto;padding:0 16px;height:56px;display:flex;align-items:center;
gap:4px}
.brand{font-weight:700;margin-right:auto}
nav{display:flex;gap:4px}
nav a{padding:6px 12px;border-radius:var(--r-ctl);color:var(--dim);text-decoration:none;
transition:background-color .15s}
nav a:hover{background:var(--selected)}
nav a.on{background:var(--selected);color:var(--fg);font-weight:600}
main{max-width:720px;margin:0 auto;padding:0 16px 64px}
a{color:var(--fg);text-decoration:underline;text-decoration-color:var(--input);
text-underline-offset:3px}
h1{font-size:22px;font-weight:700;line-height:1.4;margin:24px 0 4px}
h2{display:flex;gap:8px;font-size:14px;font-weight:600;color:var(--dim);margin:28px 4px 8px}
h2 .count{font-weight:400}
.summary{margin:24px 4px 0}
.summary .main{font-size:22px;font-weight:700;line-height:1.4}
.summary .sub{color:var(--dim)}
.summary .todo{color:var(--warn);font-weight:600}
.list{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);
overflow:hidden;margin-bottom:12px}
.row{padding:14px 16px}
.row+.row{border-top:1px solid var(--line)}
.title{font-weight:600}
.meta{font-size:14px;color:var(--dim);margin-top:2px}
.flag{font-size:14px;color:var(--warn);margin-top:4px}
.bad{color:var(--bad)}
.tag{display:inline-block;font-size:12px;padding:0 6px;border-radius:var(--r-tag);
background:var(--selected);color:var(--dim)}
.actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px}
.actions form{margin:0}
.days .end{margin-left:auto}
button,.more summary{appearance:none;display:inline-flex;align-items:center;min-height:36px;
padding:0 14px;font:inherit;font-size:14px;line-height:1;color:var(--fg);background:var(--surface);
border:1px solid var(--line);border-radius:var(--r-ctl);cursor:pointer;
transition:background-color .15s,border-color .15s,color .15s}
button:hover,.more summary:hover{background:var(--selected)}
button.primary{background:var(--primary);border-color:var(--primary);color:var(--on-primary);
font-weight:600}
button.primary:hover{background:var(--primary-hover);border-color:var(--primary-hover)}
button.quiet{background:transparent;border-color:transparent;color:var(--dim)}
button.quiet:hover{background:transparent;color:var(--bad)}
button:focus-visible,.more summary:focus-visible,input:focus-visible,a:focus-visible{
outline:2px solid var(--fg);outline-offset:2px}
.more summary{list-style:none}
.more summary::-webkit-details-marker{display:none}
.more[open]{flex-basis:100%}
.more[open] summary{background:var(--selected);font-weight:600}
.more .days{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
input[type=text],input[type=password]{min-height:40px;padding:0 12px;font:inherit;font-size:16px;
color:var(--fg);background:var(--surface);border:1px solid var(--input);border-radius:var(--r-ctl)}
.reject{display:flex;gap:8px;flex:1;min-width:220px}
.reject input{flex:1;min-width:0;min-height:36px;font-size:14px}
.empty{color:var(--dim);padding:14px 16px}
.err{margin:16px 0 0;padding:10px 14px;font-size:14px;color:var(--bad);background:var(--surface);
border:1px solid currentColor;border-radius:var(--r-ctl)}
.day{display:grid;grid-template-columns:96px 1fr;gap:12px}
.day .d{font-size:14px;color:var(--dim);padding-top:1px}
.day.today{background:var(--selected)}
.day.today .d{color:var(--fg);font-weight:600}
.day .t+.t{margin-top:4px}
.done{text-decoration:line-through;color:var(--dim)}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px}
.metrics .n{display:block;font-size:20px;font-weight:700;line-height:1.3}
.metrics .l{font-size:13px;color:var(--dim)}
details.ov{margin-top:12px}
details.ov summary{cursor:pointer;color:var(--dim);font-size:14px}
.body{white-space:pre-wrap;font-size:15px;margin-top:8px}
.row>.body:first-child{margin-top:0}
.tags{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:8px 0 16px;font-size:14px;
color:var(--dim)}
details.ov .body{padding-top:12px;border-top:1px solid var(--line)}
.login{max-width:360px;margin:15vh auto 0}
.login form{display:flex;flex-direction:column;gap:12px;margin-top:16px}
.login button{justify-content:center;min-height:40px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""
_FONTS = "https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400..700&display=swap"


def _page(title: str, active: str, body: str) -> HTMLResponse:
    tabs = [
        ("today", "/app", "今天"),
        ("week", "/app/week", "本週"),
        ("projects", "/app/projects", "專案"),
    ]
    nav = "".join(
        f'<a href="{href}"{" class=on aria-current=page" if key == active else ""}>{label}</a>'
        for key, href, label in tabs
    )
    return HTMLResponse(
        "<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        "<meta name='theme-color' content='#FFFFFF'>"
        f"<title>{escape(title)} · Axiom</title>"
        f"<link rel='stylesheet' href='{_FONTS}'><style>{_CSS}</style></head>"
        f"<body><header><div class='bar'><span class='brand'>Axiom</span><nav>{nav}</nav>"
        f"</div></header><main>{body}</main></body></html>"
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
    end = ' class="end"' if action == "drop" else ""
    return (
        f'<form method="post" action="/app/tasks/{task_id}/{action}"{end}{onsubmit}>'
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
        + _button(
            task.id,
            "defer",
            "之後再說",
            back=back,
            extra='<input type="hidden" name="to" value="someday">',
        )
        + '<details class="more"><summary>更多</summary><div class="days">'
        + days
        + _button(task.id, "drop", "刪除", back=back, cls="quiet", confirm="刪除這件事？")
        + "</div></details>"
    )
    return (
        f'<div class="row"><div class="title">{escape(task.title)}</div>'
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
        f'<div class="row"><div class="title">{escape(task.title)}</div>'
        f'{_meta(task, today)}{evidence}<div class="actions">'
        f"{_button(task.id, 'confirm', '確認', back='/app', cls='primary')}{reject}</div></div>"
    )


def _section(title: str, cards: list[str], *, empty: str | None = None) -> str:
    if not cards and empty is None:
        return ""
    inner = "".join(cards) if cards else f'<div class="empty">{empty}</div>'
    count = f'<span class="count">{len(cards)}</span>' if cards else ""
    return f'<section><h2>{title}{count}</h2><div class="list">{inner}</div></section>'


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
        status = (
            f'<div class="main">今天 {n_today} 件</div>'
            f'<div class="sub">其他 {view.arranged} 件都已排好日期</div>'
        )
        if view.unscheduled:
            status += f'<div class="todo">{len(view.unscheduled)} 件還沒安排</div>'
        error = request.query_params.get("error")
        body = f'<div class="summary">{status}</div>'
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
            body += '<div class="list"><div class="empty">今天沒有要處理的事。</div></div>'
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
                or '<div class="t meta">—</div>'
            )
            cls = "row day today" if d == today else "row day"
            cols.append(
                f'<div class="{cls}"><div class="d">週{WEEKDAYS[i]} {d.month}/{d.day}</div>'
                f"<div>{items}</div></div>"
            )
        body = f"<h1>{_day(view.monday)} – {_day(view.monday + timedelta(days=6))}</h1>"
        body += f'<div class="list">{"".join(cols)}</div>'
        groups: dict[str, list[Task]] = {}
        for t in view.done:
            groups.setdefault(t.project or "未分專案", []).append(t)
        rows = [
            f'<div class="row"><div class="title">{escape(project)}'
            f' <span class="tag">{len(items)}</span></div>'
            + "".join(f'<div class="meta">{escape(t.title)}</div>' for t in items)
            + "</div>"
            for project, items in groups.items()
        ]
        body += _section("本週已完成", rows, empty="還沒有。")
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
            metrics = "".join(
                f'<div><span class="n">{n}</span><span class="l">{label}</span></div>'
                for n, label in (
                    (s.open if s else 0, "進行中"),
                    (s.done_this_week if s else 0, "本週完成"),
                    (s.done_total if s else 0, "總完成"),
                )
            )
            due = (
                f'<div class="meta">最近截止 {_day(s.next_due, today)}</div>'
                if s and s.next_due
                else ""
            )
            flags = ""
            if s and s.open > OPEN_WARN:
                flags += (
                    f'<div class="flag">進行中 {s.open} 筆，超過 {OPEN_WARN} 筆，該收斂了</div>'
                )
            if overview and overview.name in drifted:
                newer = "、".join(drifted[overview.name].newer_related)
                flags += f'<div class="flag">總覽落後於：{escape(newer)}</div>'
            mine = [o for o in orphans if project and o.name.startswith(f"{project}-")]
            if mine:
                names = "、".join(
                    f'<a href="/app/memory/{escape(o.name)}">{escape(o.name)}</a>' for o in mine
                )
                flags += (
                    '<details class="ov">'
                    f'<summary class="flag">{len(mine)} 筆記憶沒連到總覽</summary>'
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
                    '<details class="ov">'
                    f"<summary>總覽（{overview.updated_at:%Y-%m-%d} 更新）</summary>"
                    f'<div class="body">{_inline_md(overview.content)}</div></details>'
                )
            return (
                f'<div class="list"><div class="row"><div class="title">{escape(name)}</div>'
                f'{due}<div class="metrics">{metrics}</div>{flags}{body}</div></div>'
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
        body = "<h1>專案</h1>" + (
            "".join(cards) or '<div class="list"><div class="empty">沒有專案。</div></div>'
        )
        return _page("專案", "projects", body)

    async def memory_page(request: Request) -> Response:
        if (denied := await guard(request)) is not None:
            return denied
        memories = await get_memories()
        memory = await memories.get(request.path_params["name"])
        if memory is None:
            return _page(
                "找不到", "", '<div class="list"><div class="empty">找不到這筆記憶。</div></div>'
            )
        related = "、".join(
            f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in memory.related
        )
        body = (
            f"<h1>{escape(memory.name)}</h1>"
            f'<div class="meta">{escape(memory.description)}</div>'
            f'<div class="tags"><span class="tag">{memory.type}</span>'
            f'<span class="tag">{memory.category}</span>'
            f"<span>{memory.updated_at:%Y-%m-%d} 更新</span></div>"
            f'<div class="list"><div class="row"><div class="body">'
            f"{_inline_md(memory.content)}</div></div>"
            + (f'<div class="row meta">相關：{related}</div>' if related else "")
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
            f'<div class="login"><h1>登入 Axiom</h1>'
            '<div class="meta">輸入 service token，這台裝置會記住 30 天。</div>'
            f'{error}<form method="post"><input type="password" name="token" '
            'placeholder="Service token" aria-label="Service token" '
            'autocomplete="current-password">'
            '<button class="primary">進入</button></form></div>'
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
