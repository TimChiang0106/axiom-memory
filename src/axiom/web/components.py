"""HTML pieces the /app pages are built from: task rows and their actions,
sections, memory rows, review cards and light markdown.

Every function returns a string of escaped HTML; callers pass raw values.
"""

import re
from datetime import date, timedelta
from html import escape
from urllib.parse import quote, urlencode

from axiom.models import MemorySummary, ReviewReport, Task, TaskState
from axiom.tasks import ProjectStats, later_this_week
from axiom.web.labels import (
    APP_LABELS,
    NO_PROJECT,
    TRIGGER_LABELS,
    TYPE_LABELS,
    WEEKDAYS,
)

# A leading "[P1]" or "[有時間再做]" in a title is a priority label the AI
# clients write; it is shown as a tag and used to order a section.
_LABEL = re.compile(r"^\[([^\[\]]{1,10})\]\s*")


def split_label(title: str) -> tuple[str | None, str]:
    if m := _LABEL.match(title):
        return m.group(1), title[m.end() :]
    return None, title


def rank(task: Task) -> int:
    """P0..P9 first by number, then unlabelled, then any other label
    ("有時間再做" and the like mean "not now")."""
    label, _ = split_label(task.title)
    if label is None:
        return 10
    if re.fullmatch(r"P\d", label):
        return int(label[1])
    return 11


def by_priority(tasks: list[Task]) -> list[Task]:
    return sorted(tasks, key=rank)


def title_html(title: str) -> str:
    label, rest = split_label(title)
    if label is None:
        return escape(title)
    cls = "hi" if label in ("P0", "P1") else "" if re.fullmatch(r"P\d", label) else "lo"
    return f'<span class="tag {cls}">{escape(label)}</span>{escape(rest)}'


def day(d: date, today: date | None = None) -> str:
    if today is not None:
        if d == today:
            return "今天"
        if d == today + timedelta(days=1):
            return "明天"
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def with_query(url: str, **params: str | int) -> str:
    """`url` with its query replaced by `params` — so a stale error or undo
    notice never follows the user back to the page."""
    path = url.split("?", 1)[0]
    return f"{path}?{urlencode(params)}" if params else path


def task_url(task: Task, back: str) -> str:
    return f"/app/tasks/{task.id}?{urlencode({'from': back})}"


def project_url(project: str | None) -> str:
    return f"/app/projects/{quote(project or NO_PROJECT)}"


def meta_text(
    task: Task, today: date, *, section_day: date | None = None, in_project: bool = False
) -> str:
    """`section_day` is the day the surrounding section already stands for,
    so a planned date equal to it is left out; `in_project` likewise drops
    the project name on that project's own page."""
    parts = []
    if task.code:
        parts.append(escape(task.code))
    if task.project_label and not in_project:
        parts.append(escape(task.project_label))
    if task.state and task.status in ("open", "suggested"):
        parts.append(escape(task.state))
    if task.waiting and task.waiting_on:
        parts.append(f"等 {escape(task.waiting_on)}")
    if task.planned_date and task.planned_date == section_day:
        pass
    elif task.planned_date:
        parts.append(f"{'追蹤' if task.waiting else '計畫'} {day(task.planned_date, today)}")
    elif task.someday:
        parts.append("之後再說")
    if task.due_date:
        overdue = task.due_date < today and task.status == "open"
        label = f"截止 {day(task.due_date, today)}"
        parts.append(f'<span class="bad">{label}</span>' if overdue else label)
    return "・".join(parts)


def meta(
    task: Task, today: date, *, section_day: date | None = None, in_project: bool = False
) -> str:
    text = meta_text(task, today, section_day=section_day, in_project=in_project)
    return f'<div class="meta">{text}</div>' if text else ""


def button(
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
        f'<input type="hidden" name="next" value="{escape(back)}">{extra}'
        f'<button class="{cls}">{label}</button></form>'
    )


def defer(task_id: int, label: str, to: str, *, back: str) -> str:
    return button(
        task_id, "defer", label, back=back, extra=f'<input type="hidden" name="to" value="{to}">'
    )


def task_actions(task: Task, today: date, *, back: str) -> str:
    days = "".join(defer(task.id, day(d), d.isoformat(), back=back) for d in later_this_week(today))
    return (
        button(task.id, "done", "完成", back=back, cls="primary")
        + defer(task.id, "明天", "tomorrow", back=back)
        + defer(task.id, "之後再說", "someday", back=back)
        + '<details class="more"><summary>更多</summary><div class="days">'
        + days
        + button(task.id, "drop", "刪除", back=back, cls="quiet", confirm="刪除這件事？")
        + "</div></details>"
    )


def task_card(
    task: Task,
    today: date,
    *,
    back: str = "/app",
    section_day: date | None = None,
    in_project: bool = False,
) -> str:
    """One open task: a circle to finish it, and the title, which opens the
    rest of the options — so a list of tasks reads as tasks, not buttons."""
    flags = ""
    if task.defer_count >= 3:
        flags += f'<div class="flag">延了 {task.defer_count} 次，還要做嗎？</div>'
    check = (
        f'<form method="post" action="/app/tasks/{task.id}/done" class="check">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        f'<button aria-label="完成「{escape(task.title)}」"></button></form>'
    )
    days = "".join(defer(task.id, day(d), d.isoformat(), back=back) for d in later_this_week(today))
    opts = (
        defer(task.id, "明天", "tomorrow", back=back)
        + defer(task.id, "之後再說", "someday", back=back)
        + days
        + f'<a class="btn" href="{escape(task_url(task, back))}">編輯</a>'
        + button(task.id, "drop", "刪除", back=back, cls="quiet", confirm="刪除這件事？")
    )
    return (
        f'<div class="row task">{check}<details><summary><div>'
        f'<div class="title">{title_html(task.title)}</div>'
        f"{meta(task, today, section_day=section_day, in_project=in_project)}{flags}</div>"
        f'<span class="dots" aria-hidden="true">⋯</span></summary>'
        f'<div class="opts">{opts}</div></details></div>'
    )


def done_card(task: Task, *, back: str) -> str:
    return (
        '<div class="row"><div class="actions flush">'
        f'<a class="done grow" href="{escape(task_url(task, back))}">'
        f"{escape(split_label(task.title)[1])}</a>"
        f"{button(task.id, 'reopen', '復原', back=back, cls='quiet')}</div></div>"
    )


def suggestion_card(task: Task, today: date, *, back: str = "/app") -> str:
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
        lines.append(f"{day(task.expires_at.date())} 前未處理會自動失效")
    evidence = "".join(f'<div class="meta">{line}</div>' for line in lines)
    reject = (
        f'<form method="post" action="/app/tasks/{task.id}/reject" class="reject">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        '<input type="text" name="reason" placeholder="拒絕原因（選填）" aria-label="拒絕原因">'
        "<button>拒絕</button></form>"
    )
    return (
        f'<div class="row"><div class="title">{title_html(task.title)}</div>'
        f'{meta(task, today)}{evidence}<div class="actions">'
        f"{button(task.id, 'confirm', '確認', back=back, cls='primary')}{reject}</div></div>"
    )


def section(
    title: str,
    cards: list[str],
    *,
    empty: str | None = None,
    tools: str = "",
    counted: bool = True,
    limit: int | None = None,
    grid: bool = False,
) -> str:
    """A titled list. `grid` lays its rows out as cards on a wide screen;
    `limit` shows that many and folds the rest behind a tap."""
    if not cards and empty is None:
        return ""
    inner = "".join(cards) if cards else f'<div class="empty">{empty}</div>'
    if limit is not None and len(cards) > limit:
        inner = (
            "".join(cards[:limit])
            + f'<details class="rest"><summary>顯示其餘 {len(cards) - limit} 筆</summary>'
            + f'<div class="rest-rows">{"".join(cards[limit:])}</div></details>'
        )
    count = f'<span class="count">{len(cards)}</span>' if cards and counted else ""
    cls = "list grid" if grid else "list"
    return f'<section><h2>{title}{count}</h2>{tools}<div class="{cls}">{inner}</div></section>'


def split(left: str, right: str) -> str:
    """Two columns on a wide screen (main, side); stacked on a phone."""
    if not right:
        return left
    return f'<div class="split"><div>{left}</div><div>{right}</div></div>'


def project_options(projects: dict[str, str], selected: str | None, *, none_label: str) -> str:
    options = [f'<option value="">{none_label}</option>']
    for p, name in projects.items():
        sel = " selected" if p == selected else ""
        options.append(f'<option value="{escape(p)}"{sel}>{escape(name)}</option>')
    return "".join(options)


def state_options(states: list[TaskState], selected: str | None) -> str:
    options = ['<option value="">不設定</option>']
    for s in states:
        sel = " selected" if s.name == selected else ""
        options.append(f'<option value="{escape(s.name)}"{sel}>{escape(s.name)}</option>')
    return "".join(options)


def state_row(state: TaskState, *, back: str) -> str:
    checked = " checked" if state.waiting else ""
    return (
        '<div class="row states">'
        '<form method="post" action="/app/states/save">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        f'<input type="hidden" name="rename" value="{escape(state.name)}">'
        f'<input type="text" name="name" required maxlength="20" aria-label="狀態名稱" '
        f'value="{escape(state.name)}">'
        f'<label><input type="checkbox" name="waiting"{checked}>等別人</label>'
        "<button>儲存</button></form>"
        '<form method="post" action="/app/states/delete" '
        "onsubmit=\"return confirm('刪除這個狀態？')\">"
        f'<input type="hidden" name="next" value="{escape(back)}">'
        f'<input type="hidden" name="name" value="{escape(state.name)}">'
        '<button class="quiet">刪除</button></form></div>'
    )


def new_state_row(*, back: str) -> str:
    return (
        '<div class="row states">'
        '<form method="post" action="/app/states/save">'
        f'<input type="hidden" name="next" value="{escape(back)}">'
        '<input type="text" name="name" required maxlength="20" placeholder="新增狀態" '
        'aria-label="新增狀態">'
        '<label><input type="checkbox" name="waiting">等別人</label>'
        '<button class="primary">新增</button></form></div>'
    )


def quick_add(projects: dict[str, str]) -> str:
    whens = [("today", "今天"), ("tomorrow", "明天"), ("someday", "之後"), ("none", "不排日期")]
    seg = "".join(
        f'<label><input type="radio" name="when" value="{v}"{" checked" if v == "today" else ""}>'
        f"<span>{label}</span></label>"
        for v, label in whens
    )
    project = (
        f'<select name="project" aria-label="專案">'
        f"{project_options(projects, None, none_label='不分專案')}</select>"
        if projects
        else ""
    )
    return (
        '<form method="post" action="/app/tasks/new" class="list add">'
        '<div class="row"><input class="wide" type="text" name="title" required '
        'placeholder="新增一件事" aria-label="新增一件事" autocomplete="off" enterkeyhint="done">'
        f'<div class="addopts"><div class="seg">{seg}</div>{project}'
        '<button class="primary">新增</button></div></div></form>'
    )


def metrics(s: ProjectStats | None) -> str:
    return (
        '<div class="metrics">'
        + "".join(
            f'<div><span class="n">{n}</span><span class="l">{label}</span></div>'
            for n, label in (
                (s.open if s else 0, "未完成"),
                (s.done_this_week if s else 0, "本週完成"),
                (s.done_total if s else 0, "總完成"),
            )
        )
        + "</div>"
    )


def progress(s: ProjectStats | None) -> str:
    """Where a project stands: its earliest unfinished phase, and how much of
    it is out of the user's hands."""
    parts = []
    if s and s.phase:
        parts.append(f"目前階段 {escape(s.phase)}")
    if s and s.waiting:
        parts.append(f"{s.waiting} 件等別人")
    return f'<div class="meta">{"・".join(parts)}</div>' if parts else ""


def inline_md(text: str) -> str:
    """Escape, then render **bold** and link `doc:<name>` references —
    enough for overview memories and plain-text documents."""
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escape(text))
    return re.sub(r"\bdoc:([\w.-]+)", r'<a href="/app/docs/\1">doc:\1</a>', html)


def memory_row(name: str, description: str, kind: str = "") -> str:
    label = TYPE_LABELS.get(kind, kind)
    tag = f'<div class="meta"><span class="tag">{label}</span></div>' if kind else ""
    return (
        f'<a class="row" href="/app/memory/{escape(name)}"><div class="title">'
        f'{escape(name)}</div><div class="meta clamp">{escape(description)}</div>{tag}</a>'
    )


def app(source_app: str) -> str:
    return escape(APP_LABELS.get(source_app, source_app))


def mem_link(name: str) -> str:
    return f'<a href="/app/memory/{escape(name)}">{escape(name)}</a>'


def forget(name: str, *, back: str, label: str = "刪除") -> str:
    return (
        f'<form method="post" action="/app/memory/{escape(name)}/forget" '
        f"onsubmit=\"return confirm('刪除「{escape(name)}」？"
        "AI 之後就查不到了，歷史版本會保留。')\">"
        f'<input type="hidden" name="next" value="{escape(back)}">'
        f'<button class="quiet">{label}</button></form>'
    )


def keep(kind: str, key: str, label: str) -> str:
    return (
        '<form method="post" action="/app/review/dismiss">'
        f'<input type="hidden" name="kind" value="{escape(kind)}">'
        f'<input type="hidden" name="key" value="{escape(key)}">'
        f"<button>{label}</button></form>"
    )


def review_items(
    report: ReviewReport,
    summaries: dict[str, MemorySummary],
    dismissed: set[tuple[str, str]],
) -> list[tuple[str, str, list[str]]]:
    """The review report as sections, minus what the user kept."""
    back = "/app/review"

    def desc(name: str) -> str:
        m = summaries.get(name)
        return f'<div class="meta clamp">{escape(m.description)}</div>' if m else ""

    def card(head: str, body: str, actions: str) -> str:
        return (
            f'<div class="row"><div class="title">{head}</div>{body}'
            f'<div class="actions">{actions}</div></div>'
        )

    # (title, hint for the whole section, cards)
    sections: list[tuple[str, str, list[str]]] = []
    dups = []
    for d in report.duplicates:
        key = f"{d.name_a}|{d.name_b}"
        if ("duplicate", key) in dismissed:
            continue
        sides = "".join(
            f'<div class="pair"><div class="grow">{mem_link(n)}{desc(n)}</div>'
            f"{forget(n, back=back)}</div>"
            for n in (d.name_a, d.name_b)
        )
        dups.append(
            card(
                f"相似度 {d.similarity:.0%}",
                sides,
                keep("duplicate", key, "不是重複"),
            )
        )
    sections.append(("可能重複", "要合併的話，跟 AI 說「把這兩筆合併成一筆」。", dups))

    drifted = []
    for o in report.drifted_overviews:
        key = f"{o.name}@{o.updated_at.isoformat()}"
        if ("drifted", key) in dismissed:
            continue
        newer = "、".join(mem_link(n) for n in o.newer_related)
        drifted.append(
            card(
                mem_link(o.name),
                f'<div class="meta">比它新的細節：{newer}</div>'
                f'<div class="meta">跟 AI 說「更新 {escape(o.name)}」。</div>',
                keep("drifted", key, "略過"),
            )
        )
    sections.append(("專案總覽落後", "", drifted))

    for kind, title, items, hint, keep_label in (
        (
            "orphan",
            "沒連到專案總覽",
            report.orphans,
            "可以一次交給 AI：「把 Axiom 裡沒連到總覽的記憶，整理進各自的專案總覽」。",
            "略過",
        ),
        ("headless", "已結束專案留下的", report.headless, "", "保留"),
    ):
        cards = [
            card(
                mem_link(m.name),
                desc(m.name),
                forget(m.name, back=back) + keep(kind, m.name, keep_label),
            )
            for m in items
            if (kind, m.name) not in dismissed
        ]
        sections.append((title, hint, cards))

    for kind, title, items, note, keep_label in (
        ("stale", "現況可能過時", report.stale_state, "沒更新", "還是對的"),
        ("zombie", "很少被 AI 用到", report.zombies, "後幾乎沒被用過", "保留"),
    ):
        cards = [
            card(
                mem_link(m.name),
                desc(m.name) + f'<div class="meta">{m.since:%Y-%m-%d} {note}</div>',
                forget(m.name, back=back)
                + keep(kind, f"{m.name}@{m.since.isoformat()}", keep_label),
            )
            for m in items
            if (kind, f"{m.name}@{m.since.isoformat()}") not in dismissed
        ]
        sections.append((title, "", cards))

    recurring = []
    for c in report.recurring:
        key = c.samples[0][:200] if c.samples else c.first_at.isoformat()
        if ("recurring", key) in dismissed:
            continue
        samples = "".join(f'<div class="meta clamp">・{escape(x)}</div>' for x in c.samples)
        recurring.append(
            card(
                f"{c.count} 次・{c.first_at:%m/%d}–{c.last_at:%m/%d}",
                samples,
                keep("recurring", key, "略過"),
            )
        )
    sections.append(
        ("重複出現的動態", "值得記下來的話，跟 AI 說「把這個整理成一筆記憶」。", recurring)
    )
    return sections


def prose(text: str) -> str:
    """Paragraphs and `- ` bullet lists, each line through `inline_md`."""
    html, items = [], []
    for line in [*text.strip().splitlines(), ""]:
        line = line.strip()
        if line.startswith(("- ", "* ")):
            items.append(f"<li>{inline_md(line[2:])}</li>")
            continue
        if items:
            html.append(f"<ul>{''.join(items)}</ul>")
            items = []
        if heading := re.match(r"#{1,6}\s+(.*)", line):
            html.append(f"<h3>{inline_md(heading.group(1))}</h3>")
        elif line:
            html.append(f"<p>{inline_md(line)}</p>")
    return f'<div class="prose">{"".join(html)}</div>'


def profile_sections(text: str) -> list[tuple[str, str]]:
    """Split a profile at its `## ` headings into (heading, body) pairs; text
    before the first heading gets an empty heading."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b)) for h, b in sections if h or "".join(b).strip()]
