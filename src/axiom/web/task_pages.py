"""Task pages: today, the week, projects, a single task, and the forms
that add, finish, defer, edit and undo tasks."""

from datetime import date, timedelta
from html import escape
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from axiom.models import Memory, Task
from axiom.tasks import CLEARABLE, TaskError
from axiom.web import components as ui
from axiom.web.context import WebContext
from axiom.web.labels import (
    NO_PROJECT,
    OPEN_WARN,
    STATUS_LABELS,
    WEEKDAYS,
)
from axiom.web.layout import page


def routes(ctx: WebContext) -> list[Route]:
    async def today_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        view = await tasks.today_view()
        today = view.today
        n_today = len(view.overdue) + len(view.due_today)
        status = (
            f'<div class="date">{ui.day(today)}</div>'
            f'<div class="main">今天 {n_today} 件</div>'
            f'<div class="sub">其他 {view.arranged} 件都已排好日期</div>'
        )
        if view.unscheduled:
            status += f'<div class="todo">{len(view.unscheduled)} 件還沒安排</div>'
        body = f'<div class="summary">{status}</div>'
        body += await ctx.notices(request, tasks)
        main = ui.quick_add(await tasks.known_projects())
        bulk = ""
        if len(view.overdue) >= 2:
            bulk = (
                '<div class="tools">'
                + "".join(
                    '<form method="post" action="/app/tasks/bulk">'
                    f'<input type="hidden" name="to" value="{to}"><button>{label}</button></form>'
                    for to, label in (("today", "全部移到今天"), ("someday", "全部改到之後"))
                )
                + "</div>"
            )
        main += ui.section(
            "逾期", [ui.task_card(t, today) for t in ui.by_priority(view.overdue)], tools=bulk
        )
        main += ui.section(
            "今天",
            [ui.task_card(t, today, section_day=today) for t in ui.by_priority(view.due_today)],
        )
        main += ui.section("還沒安排", [ui.task_card(t, today) for t in view.unscheduled])
        if not (view.overdue or view.due_today or view.unscheduled or view.suggested):
            main += '<div class="list"><div class="empty">今天沒有要處理的事。</div></div>'
        side = ui.section("AI 建議待確認", [ui.suggestion_card(t, today) for t in view.suggested])
        side += ui.section("等別人", [ui.task_card(t, today) for t in view.waiting])
        side += ui.section(
            "今天已完成", [ui.done_card(t, back="/app") for t in await tasks.done_today()]
        )
        # On a wide screen the side column stays, so it says what goes there.
        side = side or '<div class="desk meta summary">完成的事和 AI 建議會出現在這裡。</div>'
        body += ui.split(main, side)
        return page("今天", "today", body)

    async def week_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        today = await tasks.today()
        try:
            day = date.fromisoformat(request.query_params.get("d", ""))
        except ValueError:
            day = today
        view = await tasks.week_view(day)
        back = ctx.here(request)
        cols = []
        for i, day_tasks in enumerate(view.days):
            d = view.monday + timedelta(days=i)
            items = (
                "".join(
                    f'<div class="t{" done" if t.status == "done" else ""}">'
                    f'<a href="{escape(ui.task_url(t, back))}">{ui.title_html(t.title)}</a></div>'
                    for t in day_tasks
                )
                or '<div class="t meta">—</div>'
            )
            cls = "row day today" if d == today else "row day"
            cols.append(
                f'<div class="{cls}"><div class="d">週{WEEKDAYS[i]} {d.month}/{d.day}</div>'
                f"<div>{items}</div></div>"
            )
        prev_week = (view.monday - timedelta(days=7)).isoformat()
        next_week = (view.monday + timedelta(days=7)).isoformat()
        body = (
            f'<div class="weeknav"><a href="/app/week?d={prev_week}">‹ 上週</a>'
            f"<h1>{ui.day(view.monday)} – {ui.day(view.monday + timedelta(days=6))}</h1>"
            f'<a href="/app/week?d={next_week}">下週 ›</a></div>'
        )
        body += await ctx.notices(request, tasks)
        body += f'<div class="list week">{"".join(cols)}</div>'
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
        body += ui.section("這週已完成", rows, empty="還沒有。", grid=True)
        return page("本週", "week", body)

    async def projects_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        memories = await ctx.get_memories()
        today = await tasks.today()
        stats = await tasks.project_stats()
        overviews = await memories.overviews()
        drifted = {d.name: d for d in await memories.drifted_overviews()}
        orphans = await memories.orphans()

        def card(project: str | None, overview: Memory | None) -> str:
            s = stats.get(project)
            name = project or "未分專案"
            due = (
                f'<div class="meta">最近截止 {ui.day(s.next_due, today)}</div>'
                if s and s.next_due
                else ""
            )
            flags = ""
            if s and s.open > OPEN_WARN:
                flags += (
                    f'<div class="flag">未完成 {s.open} 筆，超過 {OPEN_WARN} 筆，該收斂了</div>'
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
            return (
                f'<div class="list"><div class="row"><div class="title">'
                f'<a href="{ui.project_url(project)}">{escape(name)} ›</a></div>'
                f"{ui.progress(s)}{due}{ui.metrics(s)}{flags}</div></div>"
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
            f'<div class="cards boxes">{"".join(cards)}</div>'
            if cards
            else '<div class="list"><div class="empty">沒有專案。</div></div>'
        )
        return page("專案", "projects", body)

    async def project_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        memories = await ctx.get_memories()
        documents = await ctx.get_documents()
        raw = request.path_params["name"]
        project = None if raw == NO_PROJECT else raw
        today = await tasks.today()
        stats = await tasks.project_stats()
        overview = await memories.get(f"{project}-overview") if project else None
        if project and overview is None and project not in stats:
            return page("找不到", "projects", '<div class="empty">找不到這個專案。</div>')
        back = ctx.here(request)

        found = await tasks.find(project=project, statuses=["open", "suggested", "done"])
        if project is None:
            found = [t for t in found if t.project is None]
        open_ = [t for t in found if t.status == "open"]
        suggested = [t for t in found if t.status == "suggested"]
        done = sorted(
            (t for t in found if t.status == "done"),
            key=lambda t: t.done_at or t.updated_at,
            reverse=True,
        )[:10]
        body = '<a class="back" href="/app/projects">‹ 專案</a>'
        body += f"<h1>{escape(project or '未分專案')}</h1>"
        body += await ctx.notices(request, tasks)
        s = stats.get(project)
        body += (
            f'<div class="list"><div class="row flush">{ui.progress(s)}{ui.metrics(s)}</div></div>'
        )

        def open_section(title: str, items: list[Task], empty: str | None = None) -> str:
            cards = [
                ui.task_card(t, today, back=back, in_project=True) for t in ui.by_priority(items)
            ]
            return ui.section(escape(title), cards, empty=empty)

        phases = sorted({t.phase for t in open_ if t.phase})
        if phases:
            main = "".join(open_section(p, [t for t in open_ if t.phase == p]) for p in phases)
            main += open_section("未分階段", [t for t in open_ if not t.phase])
        else:
            main = open_section("未完成", open_, empty="沒有未完成的事。")
        main += ui.section(
            "AI 建議待確認", [ui.suggestion_card(t, today, back=back) for t in suggested]
        )
        main += ui.section("最近完成", [ui.done_card(t, back=back) for t in done])
        side = ""
        if overview:
            side += ui.section(
                "總覽",
                [
                    f'<div class="row"><div class="body">{ui.inline_md(overview.content)}</div>'
                    f'<div class="meta foot">'
                    f'<a href="/app/memory/{escape(overview.name)}">{escape(overview.name)}</a>'
                    f"・{overview.updated_at:%Y-%m-%d} 更新</div></div>"
                ],
                counted=False,
            )
        if project:
            docs = await documents.list_all(project=project)
            side += ui.section(
                "文件",
                [
                    f'<a class="row" href="/app/docs/{escape(d.name)}"><div class="title">'
                    f'{escape(d.title)}</div><div class="meta">{d.chars} 字・'
                    f"{d.updated_at:%Y-%m-%d} 更新</div></a>"
                    for d in docs
                ],
            )
        body += ui.split(main, side)
        return page(project or "未分專案", "projects", body)

    async def task_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        task = await tasks.get(int(request.path_params["task_id"]))
        back = ctx.back_to(request.query_params.get("from"))
        top = f'<a class="back" href="{escape(back)}">‹ 返回</a>'
        if task is None:
            return page("找不到", "", top + '<div class="empty">找不到這件事。</div>')
        today = await tasks.today()
        projects = await tasks.known_projects()
        body = (
            f"{top}<h1>{escape(task.title)}</h1>"
            f'<div class="tags"><span class="tag">{STATUS_LABELS.get(task.status, task.status)}'
            f"</span><span>{ui.meta_text(task, today)}</span></div>"
        )
        body += await ctx.notices(request, tasks)
        if task.status == "open":
            body += (
                f'<div class="list"><div class="row"><div class="actions flush">'
                f"{ui.task_actions(task, today, back=back)}</div></div></div>"
            )
        elif task.status == "suggested":
            body += f'<div class="list">{ui.suggestion_card(task, today, back=back)}</div>'
        elif task.status in ("done", "dropped"):
            when = f"{task.done_at:%Y-%m-%d} 完成・" if task.done_at else ""
            body += (
                f'<div class="list"><div class="row"><div class="actions flush">'
                f'<span class="meta grow">{when}可以改回未完成</span>'
                f"{ui.button(task.id, 'reopen', '復原', back=ctx.here(request))}</div></div></div>"
            )
        if task.status in ("open", "suggested"):

            def date_value(d: date | None) -> str:
                return d.isoformat() if d else ""

            def text_field(
                label: str, name: str, value: str | None, limit: int, extra: str = ""
            ) -> str:
                return (
                    f'<label class="field">{label}<input type="text" name="{name}" '
                    f'maxlength="{limit}" value="{escape(value or "")}"{extra}></label>'
                )

            states = await tasks.states()
            phases = "".join(
                f'<option value="{escape(p)}">' for p in await tasks.phases(task.project)
            )
            manage = f"/app/states?{urlencode({'from': ctx.here(request)})}"
            body += (
                "<h2>編輯</h2>"
                f'<form method="post" action="/app/tasks/{task.id}/edit" class="list">'
                f'<input type="hidden" name="next" value="{escape(ctx.here(request))}">'
                '<div class="row"><div class="fields">'
                '<label class="field full">標題<input type="text" name="title" required '
                f'value="{escape(task.title)}"></label>'
                '<label class="field">計畫日期<input type="date" name="planned_date" '
                f'value="{date_value(task.planned_date)}"></label>'
                '<label class="field">截止日<input type="date" name="due_date" '
                f'value="{date_value(task.due_date)}"></label>'
                '<label class="field full">專案<select name="project">'
                f"{ui.project_options(projects, task.project, none_label='不分專案')}"
                "</select></label>"
                '<label class="field">狀態<select name="state">'
                f"{ui.state_options(states, task.state)}</select></label>"
                f"{text_field('等誰', 'waiting_on', task.waiting_on if task.waiting else '', 100)}"
                f"{text_field('階段', 'phase', task.phase, 40, ' list="phase-list"')}"
                f"{text_field('編號', 'code', task.code, 20)}"
                f'<datalist id="phase-list">{phases}</datalist>'
                '<div class="field full meta">狀態是等別人時，計畫日期就是要追蹤的那天。'
                f'<a href="{escape(manage)}">管理狀態</a></div>'
                '</div><div class="actions"><button class="primary">儲存</button></div></div>'
                "</form>"
            )
        if task.evidence and task.status != "suggested":
            ev = task.evidence
            if ev.memories:
                links = "、".join(
                    f'<a href="/app/memory/{escape(n)}">{escape(n)}</a>' for n in ev.memories
                )
                body += f'<div class="meta">AI 建議，依據：{links}</div>'
        body += (
            f'<div class="meta foot">{task.created_at:%Y-%m-%d} 建立'
            f"・{task.updated_at:%Y-%m-%d} 更新</div>"
        )
        return page(task.title, "", f'<div class="narrow">{body}</div>')

    async def new_task(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        back = ctx.back_to(str(form.get("next") or ""))
        tasks = await ctx.get_tasks()
        today = await tasks.today()
        when = str(form.get("when") or "")
        planned = {"today": today, "tomorrow": today + timedelta(days=1)}.get(when)
        try:
            await tasks.add(
                title=str(form.get("title") or ""),
                project=str(form.get("project") or "") or None,
                planned_date=planned,
                someday=when == "someday",
            )
        except TaskError as e:
            return ctx.redirect(back, error=str(e))
        return ctx.redirect(back)

    async def bulk_action(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        to = str(form.get("to") or "")
        tasks = await ctx.get_tasks()
        view = await tasks.today_view()
        try:
            for t in view.overdue:
                await tasks.defer(t.id, to=view.today if to == "today" else "someday")
        except TaskError as e:
            return ctx.redirect("/app", error=str(e))
        return ctx.redirect("/app")

    async def task_action(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        back = ctx.back_to(str(form.get("next") or ""))
        tasks = await ctx.get_tasks()
        task_id = int(request.path_params["task_id"])
        action = request.path_params["action"]
        try:
            if action == "done":
                await tasks.done(task_id)
            elif action == "drop":
                await tasks.drop(task_id)
            elif action == "reopen":
                await tasks.reopen(task_id)
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
            elif action == "edit":
                fields = {
                    k: str(form.get(k) or "").strip()
                    for k in (
                        "title",
                        "project",
                        "planned_date",
                        "due_date",
                        "state",
                        "waiting_on",
                        "phase",
                        "code",
                    )
                }
                # The field stays on screen after picking a state that does
                # not wait; what it says then no longer applies.
                waits = {s.name for s in await tasks.states() if s.waiting}
                if fields["state"] not in waits:
                    fields["waiting_on"] = ""
                await tasks.update(
                    task_id,
                    title=fields["title"],
                    project=fields["project"] or None,
                    planned_date=date.fromisoformat(fields["planned_date"])
                    if fields["planned_date"]
                    else None,
                    due_date=date.fromisoformat(fields["due_date"]) if fields["due_date"] else None,
                    state=fields["state"] or None,
                    waiting_on=fields["waiting_on"] or None,
                    phase=fields["phase"] or None,
                    code=fields["code"] or None,
                    clear=[k for k in CLEARABLE if not fields[k]],
                )
            else:
                return HTMLResponse("Unknown action.", status_code=404)
        except (TaskError, ValueError) as e:
            return ctx.redirect(back, error=str(e))
        if action in ("done", "drop"):
            return ctx.redirect(back, undo=task_id)
        return ctx.redirect(back)

    async def states_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        tasks = await ctx.get_tasks()
        back = ctx.back_to(request.query_params.get("from"))
        here = ctx.here(request)
        rows = [ui.state_row(s, back=here) for s in await tasks.states()]
        body = (
            f'<a class="back" href="{escape(back)}">‹ 返回</a><h1>工作狀態</h1>'
            '<div class="meta">標成等別人的狀態不算逾期，計畫日期當天才回到今天。</div>'
        )
        body += await ctx.notices(request, tasks)
        body += f'<div class="list">{"".join(rows)}{ui.new_state_row(back=here)}</div>'
        return page("工作狀態", "me", f'<div class="narrow">{body}</div>')

    async def save_state(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        back = ctx.back_to(str(form.get("next") or ""))
        tasks = await ctx.get_tasks()
        try:
            await tasks.save_state(
                str(form.get("name") or ""),
                waiting="waiting" in form,
                rename=str(form.get("rename") or "") or None,
            )
        except TaskError as e:
            return ctx.redirect(back, error=str(e))
        return ctx.redirect(back)

    async def delete_state(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        back = ctx.back_to(str(form.get("next") or ""))
        tasks = await ctx.get_tasks()
        try:
            await tasks.delete_state(str(form.get("name") or ""))
        except TaskError as e:
            return ctx.redirect(back, error=str(e))
        return ctx.redirect(back)

    return [
        Route("/app", today_page, methods=["GET"]),
        Route("/app/week", week_page, methods=["GET"]),
        Route("/app/projects", projects_page, methods=["GET"]),
        Route("/app/projects/{name}", project_page, methods=["GET"]),
        Route("/app/tasks/new", new_task, methods=["POST"]),
        Route("/app/tasks/bulk", bulk_action, methods=["POST"]),
        Route("/app/tasks/{task_id:int}", task_page, methods=["GET"]),
        Route("/app/tasks/{task_id:int}/{action}", task_action, methods=["POST"]),
        Route("/app/states", states_page, methods=["GET"]),
        Route("/app/states/save", save_state, methods=["POST"]),
        Route("/app/states/delete", delete_state, methods=["POST"]),
    ]
