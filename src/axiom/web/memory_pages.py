"""Memory pages: the library and search, a single memory or document, the
review inbox, the "我" page, and the backup download."""

import io
import zipfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from html import escape

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from axiom.export import render_index, render_memory
from axiom.models import DocumentSummary, Memory, MemorySummary
from axiom.web import components as ui
from axiom.web.context import WebContext
from axiom.web.labels import (
    CATEGORY_LABELS,
    PROFILE,
    TYPE_LABELS,
)
from axiom.web.layout import page

# The library is one table of memories and documents; "doc" is the kind
# documents file under, next to the memory types.
_KIND_LABELS = {**TYPE_LABELS, "doc": "文件"}
_KIND_ORDER = {k: n for n, k in enumerate(_KIND_LABELS)}
_PAGE_SIZES = (25, 50, 100)
_SEARCH_LIMIT = 100
# Each sortable column and the direction its first click sorts in.
_SORTS = {"name": "asc", "kind": "asc", "updated": "desc"}


@dataclass
class _Item:
    href: str
    name: str
    sub: str
    kind: str
    category: str
    updated_at: datetime


def _memory_item(m: Memory | MemorySummary) -> _Item:
    """Read by its title — or, on a memory older than titles, its
    description — with the slug underneath for when the AI names it."""
    return _Item(f"/app/memory/{m.name}", m.label, m.name, m.type, m.category, m.updated_at)


def _doc_item(d: DocumentSummary) -> _Item:
    sub = f"{d.project + '・' if d.project else ''}{d.chars} 字"
    return _Item(f"/app/docs/{d.name}", d.title, sub, "doc", "", d.updated_at)


@dataclass
class _LibraryView:
    """The library's filters, sort and page, as read from and written back
    to the query string — defaults are left out so URLs stay short."""

    q: str = ""
    c: str = ""
    t: str = ""
    s: str = ""
    d: str = ""
    p: int = 1
    n: int = 50

    @classmethod
    def parse(cls, params: Mapping[str, str]) -> "_LibraryView":
        def number(key: str, default: int) -> int:
            raw = params.get(key, "")
            return int(raw) if raw.isdigit() and int(raw) > 0 else default

        n = number("n", cls.n)
        return cls(
            q=params.get("q", "").strip(),
            c=params.get("c", "") if params.get("c") in CATEGORY_LABELS else "",
            t=params.get("t", "") if params.get("t") in _KIND_LABELS else "",
            s=params.get("s", "") if params.get("s") in _SORTS else "",
            d=params.get("d", "") if params.get("d") in ("asc", "desc") else "",
            p=number("p", 1),
            n=n if n in _PAGE_SIZES else cls.n,
        )

    @property
    def sort(self) -> str:
        """The column in force; "" is search relevance."""
        return self.s or ("" if self.q else "updated")

    @property
    def direction(self) -> str:
        return self.d or _SORTS.get(self.sort, "asc")

    def url(self, **changes: str | int) -> str:
        state = {**asdict(self), **changes}
        defaults = asdict(_LibraryView())
        return ui.with_query("/app/library", **{k: v for k, v in state.items() if v != defaults[k]})

    def sorted(self, items: list[_Item]) -> list[_Item]:
        desc = self.direction == "desc"
        if self.sort == "name":
            return sorted(items, key=lambda i: i.name.casefold(), reverse=desc)
        if self.sort == "kind":
            by_date = sorted(items, key=lambda i: i.updated_at, reverse=True)
            return sorted(by_date, key=lambda i: _KIND_ORDER[i.kind], reverse=desc)
        if self.sort == "updated":
            return sorted(items, key=lambda i: i.updated_at, reverse=desc)
        return items


def _filters(view: _LibraryView) -> str:
    kinds = '<option value="">全部種類</option>' + "".join(
        f'<option value="{k}"{" selected" if k == view.t else ""}>{label}</option>'
        for k, label in _KIND_LABELS.items()
    )
    sizes = "".join(
        f'<option value="{n}"{" selected" if n == view.n else ""}>每頁 {n} 筆</option>'
        for n in _PAGE_SIZES
    )
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{escape(v)}">'
        for k, v in (("c", view.c), ("s", view.s), ("d", view.d))
        if v
    )
    submit = ' onchange="this.form.requestSubmit()"'
    return (
        '<form class="search filters" method="get" action="/app/library">'
        f'<input type="search" name="q" value="{escape(view.q)}" placeholder="搜尋記憶與文件" '
        'aria-label="搜尋記憶與文件" enterkeyhint="search">'
        '<button class="primary">搜尋</button>'
        f'<select name="t" aria-label="種類"{submit}>{kinds}</select>'
        f'<select name="n" aria-label="每頁筆數"{submit}>{sizes}</select>'
        f"{hidden}"
        '<a class="btn" href="/app/review" '
        'title="重複、過時、沒連到總覽的記憶，和 AI 最近記下的動態">整理記憶</a></form>'
    )


def _table(view: _LibraryView, rows: list[_Item], ctx: WebContext) -> str:
    def head(key: str, label: str, cls: str) -> str:
        on = view.sort == key
        flip = "desc" if view.direction == "asc" else "asc"
        url = view.url(s=key, d=flip if on else _SORTS[key], p=1)
        arrow = ("↑" if view.direction == "asc" else "↓") if on else ""
        sort = f' aria-sort="{"ascending" if view.direction == "asc" else "descending"}"'
        return (
            f'<th class="{cls}"{sort if on else ""}>'
            f'<a href="{escape(url)}">{label}<span class="arrow">{arrow}</span></a></th>'
        )

    if not rows:
        return '<div class="list lib-wrap"><div class="empty">沒有符合的資料。</div></div>'
    body = "".join(
        f'<tr><td class="name"><a class="clamp" href="{escape(i.href)}">{escape(i.name)}</a>'
        f'<div class="meta">{escape(i.sub)}</div></td>'
        f'<td class="kind"><span class="tag">{_KIND_LABELS[i.kind]}</span></td>'
        f'<td class="cat">{CATEGORY_LABELS.get(i.category, "—")}</td>'
        f'<td class="when"><time title="{ctx.local(i.updated_at)}">'
        f"{ctx.local(i.updated_at)[:10]}</time></td></tr>"
        for i in rows
    )
    return (
        '<div class="list lib-wrap"><table class="lib"><thead><tr>'
        + head("name", "名稱", "name")
        + head("kind", "種類", "kind")
        + '<th class="cat">分類</th>'
        + head("updated", "更新時間", "when")
        + f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _pager(view: _LibraryView, p: int, pages: int, total: int) -> str:
    if not total:
        return ""
    first = (p - 1) * view.n + 1
    summary = f'<span class="meta">第 {first}–{min(p * view.n, total)} 筆，共 {total} 筆</span>'
    if pages == 1:
        return f'<div class="pager">{summary}</div>'

    def link(to: int, label: str, aria: str = "") -> str:
        extra = f' aria-label="{aria}"' if aria else ""
        if to == p:
            return f'<span class="btn on" aria-current="page">{label}</span>'
        return f'<a class="btn" href="{escape(view.url(p=to))}"{extra}>{label}</a>'

    shown = sorted({1, pages, *range(max(1, p - 2), min(pages, p + 2) + 1)})
    numbers, last = [], 0
    for n in shown:
        if n - last > 1:
            numbers.append('<span class="gap">…</span>')
        numbers.append(link(n, str(n)))
        last = n
    prev = (
        link(p - 1, "‹ 上一頁")
        if p > 1
        else '<span class="btn off" aria-disabled="true">‹ 上一頁</span>'
    )
    nxt = (
        link(p + 1, "下一頁 ›")
        if p < pages
        else '<span class="btn off" aria-disabled="true">下一頁 ›</span>'
    )
    return (
        f'<nav class="pager" aria-label="分頁">{summary}'
        f'<div class="pages">{prev}{"".join(numbers)}{nxt}</div></nav>'
    )


def routes(ctx: WebContext) -> list[Route]:
    async def library_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        documents = await ctx.get_documents()
        view = _LibraryView.parse(request.query_params)
        if view.q:
            found = await memories.recall(view.q, limit=_SEARCH_LIMIT, track_usage=False)
            needle = view.q.casefold()
            docs = [
                d
                for d in await documents.list_all()
                if needle in f"{d.name} {d.title} {d.project or ''}".casefold()
            ]
            items = [_doc_item(d) for d in docs] + [_memory_item(m) for m in found]
        else:
            items = [_doc_item(d) for d in await documents.list_all()] + [
                _memory_item(m) for m in await memories.list_all()
            ]

        typed = [i for i in items if not view.t or i.kind == view.t]
        counts = Counter(i.category for i in typed)
        shown = [i for i in typed if not view.c or i.category == view.c]
        shown = view.sorted(shown)
        pages = max(1, -(-len(shown) // view.n))
        p = min(view.p, pages)
        rows = shown[(p - 1) * view.n : p * view.n]

        body = "<h1>資料</h1>" + _filters(view)
        body += await ctx.notices(request, await ctx.get_tasks())
        chips = [("", "全部", len(typed))] + [
            (key, label, counts[key]) for key, label in CATEGORY_LABELS.items() if counts[key]
        ]
        body += (
            '<div class="chips" role="navigation" aria-label="記憶分類">'
            + "".join(
                f'<a href="{escape(view.url(c=key, p=1))}"'
                f"{' class=on aria-current=page' if key == view.c else ''}>"
                f"{label}<span>{n}</span></a>"
                for key, label, n in chips
            )
            + "</div>"
        )
        body += _table(view, rows, ctx)
        body += _pager(view, p, pages, len(shown))
        title = f"搜尋 {view.q}" if view.q else CATEGORY_LABELS.get(view.c, "資料")
        return page(title, "library", body)

    async def me_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        profile = await memories.get(PROFILE)
        mine = [m for m in await memories.list_all(category="you") if m.name != PROFILE]
        body = "<h1>關於我</h1>"
        if profile is None:
            body += (
                '<div class="list"><div class="empty">還沒有個人總覽。跟 AI 說'
                "「幫我整理 Axiom 的關於我」，它會從下面的記憶整理出來。</div></div>"
            )
        else:
            body += f'<div class="meta">AI 整理・{profile.updated_at:%Y-%m-%d} 更新</div>'
            newer = [m for m in mine if m.updated_at > profile.updated_at]
            if newer:
                names = "、".join(
                    f'<a href="/app/memory/{escape(m.name)}">{escape(m.name)}</a>' for m in newer
                )
                body += (
                    '<details class="ov"><summary class="flag">'
                    f"有 {len(newer)} 筆關於你的記憶比總覽新，可以請 AI 重新整理</summary>"
                    f'<div class="meta">{names}</div></details>'
                )
            cards = ""
            for heading, text in ui.profile_sections(profile.content):
                card = f'<div class="row">{ui.prose(text)}</div>'
                cards += (
                    ui.section(escape(heading), [card], counted=False)
                    if heading
                    else f'<div class="list">{card}</div>'
                )
            body += f'<div class="cards two">{cards}</div>'
        groups: dict[str, list[str]] = {}
        for m in mine:
            row = ui.memory_row(m.name, m.description, title=m.title)
            groups.setdefault(m.type, []).append(row)
        body += (
            '<div class="cards">'
            + "".join(
                ui.section(label, groups.get(kind, [])) for kind, label in TYPE_LABELS.items()
            )
            + "</div>"
        )
        body += (
            '<div class="meta foot">想更新時，跟 ChatGPT 或 Claude 說'
            "「幫我整理 Axiom 的關於我」。</div>"
        )
        extras = ui.section(
            "連線的 AI",
            [
                f'<div class="row"><div class="title">{ui.app(a.source_app)}</div>'
                f'<div class="meta">{a.memories} 筆記憶・{a.events} 則動態・'
                f"最後寫入 {ctx.local(a.last_at)}</div></div>"
                for a in await memories.app_activity()
            ],
            counted=False,
        )
        rows = (
            ("時區", ctx.settings.timezone),
            ("AI 建議任務", f"每天最多 {ctx.settings.suggestion_daily_limit} 件"),
            ("沒回應的建議", f"{ctx.settings.suggestion_ttl_days} 天後自動失效"),
            ("現況類記憶", f"{ctx.settings.stale_state_days} 天沒更新就提醒"),
        )
        extras += ui.section(
            "設定",
            [
                f'<div class="row"><div class="actions flush"><span class="grow">{k}</span>'
                f'<span class="meta">{escape(v)}</span></div></div>'
                for k, v in rows
            ]
            + [
                '<a class="row" href="/app/states?from=/app/me">'
                '<div class="title">工作狀態 ›</div></a>'
            ],
            tools='<div class="meta hint">要改設定，跟 Claude Code 說。</div>',
            counted=False,
        )
        extras += ui.section(
            "備份",
            [
                '<div class="row"><div class="actions flush"><span class="meta grow">'
                "全部記憶和文件，Markdown 格式</span>"
                '<a class="btn" href="/app/export.zip" download>下載 .zip</a></div></div>'
            ],
            counted=False,
        )
        body += f'<div class="cards">{extras}</div>'
        return page("關於我", "me", body)

    async def review_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        report = await memories.consolidation_candidates(
            stale_state_days=ctx.settings.stale_state_days,
            zombie_days=ctx.settings.zombie_days,
            zombie_max_use_count=ctx.settings.zombie_max_use_count,
            event_window_days=ctx.settings.event_window_days,
            event_min_count=ctx.settings.event_min_count,
        )
        summaries = {m.name: m for m in await memories.list_all()}
        sections = ui.review_items(report, summaries, await memories.dismissed())
        total = sum(len(cards) for _, _, cards in sections)
        body = '<a class="back" href="/app/library">‹ 資料</a><h1>整理記憶</h1>'
        body += (
            f'<div class="meta">{total} 筆值得看一下。刪除或保留後就不會再出現。</div>'
            if total
            else '<div class="meta">都整理好了。</div>'
        )
        body += await ctx.notices(request, await ctx.get_tasks())
        for title, hint, cards in sections:
            tools = f'<div class="meta hint">{hint}</div>' if hint and cards else ""
            body += ui.section(title, cards, tools=tools, limit=6, grid=True)
        body += ui.section(
            "AI 最近記下的動態",
            [
                f'<div class="row"><div class="clamp">{escape(e.content)}</div>'
                f'<div class="meta">{ui.app(e.source_app)}・{ctx.local(e.created_at)}</div></div>'
                for e in await memories.recent_events()
            ],
            empty="還沒有。",
            grid=True,
        )
        return page("整理記憶", "library", body)

    async def forget_memory(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        back = ctx.back_to(str(form.get("next") or ""))
        name = request.path_params["name"]
        if not await (await ctx.get_memories()).forget(name):
            return ctx.redirect(back, error=f"找不到記憶「{name}」")
        return ctx.redirect(back, forgot=name)

    async def dismiss_review(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        form = await request.form()
        kind, key = str(form.get("kind") or ""), str(form.get("key") or "")
        if kind and key:
            await (await ctx.get_memories()).dismiss(kind, key)
        return ctx.redirect("/app/review")

    async def export_zip(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        documents = await ctx.get_documents()
        everything = await memories.all_memories()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("MEMORY.md", render_index(everything))
            for m in everything:
                z.writestr(f"{m.category}/{m.name}.md", render_memory(m))
            for d in await documents.list_all():
                if doc := await documents.get(d.name):
                    z.writestr(f"docs/{doc.name}.md", f"# {doc.title}\n\n{doc.body}\n")
        stamp = f"{datetime.now(ctx.zone):%Y-%m-%d}"
        return Response(
            buf.getvalue(),
            media_type="application/zip",
            headers={"content-disposition": f'attachment; filename="axiom-{stamp}.zip"'},
        )

    async def memory_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        memory = await memories.get(request.path_params["name"])
        back = '<a class="back" href="/app/library">‹ 資料</a>'
        if memory is None:
            return page(
                "找不到",
                "library",
                back + '<div class="list"><div class="empty">找不到這筆記憶。</div></div>',
            )
        related = "、".join(ui.mem_link(n) for n in memory.related)
        used = (
            f"AI 用過 {memory.use_count} 次，最後一次 {ctx.local(memory.last_used_at)}"
            if memory.use_count and memory.last_used_at
            else "AI 還沒用過"
        )
        said = "你說的" if memory.provenance == "stated" else "AI 推測的"
        body = (
            f"{back}<h1>{escape(memory.title or memory.name)}</h1>"
            f'<div class="meta">{escape(memory.description)}</div>'
            f'<div class="tags"><span class="tag">{TYPE_LABELS[memory.type]}</span>'
            f'<span class="tag">{CATEGORY_LABELS[memory.category]}</span>'
            f"<span>{escape(memory.name) + '・' if memory.title else ''}"
            f"{said}・{ui.app(memory.source_app)} 最後寫入</span></div>"
        )
        body += await ctx.notices(request, await ctx.get_tasks())
        if memory.possibly_stale:
            body += '<div class="flag">可能過時：它連到的記憶有更新，或是太久沒確認。</div>'
        main = (
            f'<div class="list add"><div class="row">{ui.prose(memory.content)}</div>'
            + (f'<div class="row meta">相關：{related}</div>' if related else "")
            + "</div>"
        )
        side = (
            '<div class="list add"><div class="row"><div class="meta">'
            f"{ctx.local(memory.created_at)} 建立・{ctx.local(memory.updated_at)} 更新</div>"
            f'<div class="meta">{used}</div></div></div>'
        )
        side += ui.section(
            "被這些記憶連結",
            [
                ui.memory_row(m.name, m.description, title=m.title)
                for m in await memories.backlinks(memory.name)
            ],
        )
        versions = await memories.history(memory.name)
        side += ui.section(
            "歷史版本",
            [
                f'<div class="row"><details class="ver"><summary>'
                f"{ctx.local(v.written_at)}・{ui.app(v.source_app)}</summary>"
                f'<div class="meta">{escape(v.description)}</div>'
                f'<div class="body">{ui.prose(v.content)}</div></details></div>'
                for v in versions
                if v.reason == "updated"
            ],
        )
        side += (
            '<div class="list"><div class="row"><div class="actions flush">'
            '<span class="meta grow">內容要改的話，跟 AI 說要怎麼改。</span>'
            f"{ui.forget(memory.name, back='/app/library')}</div></div></div>"
        )
        body += ui.split(main, side)
        return page(memory.title or memory.name, "library", body)

    async def document_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        documents = await ctx.get_documents()
        doc = await documents.get(request.path_params["name"])
        back = '<a class="back" href="/app/library">‹ 資料</a>'
        if doc is None:
            return page(
                "找不到",
                "library",
                back + '<div class="list"><div class="empty">找不到這份文件。</div></div>',
            )
        project = (
            f'<a class="tag" href="{ui.project_url(doc.project)}">{escape(doc.project)}</a>'
            if doc.project
            else ""
        )
        body = (
            f"{back}<h1>{escape(doc.title)}</h1>"
            f'<div class="tags">{project}<span>{escape(doc.name)}</span>'
            f"<span>{len(doc.body)} 字・{doc.updated_at:%Y-%m-%d} 更新</span></div>"
            f'<div class="list"><div class="row"><div class="body">{ui.inline_md(doc.body)}'
            "</div></div></div>"
        )
        return page(doc.title, "library", f'<div class="narrow">{body}</div>')

    return [
        Route("/app/library", library_page, methods=["GET"]),
        Route("/app/me", me_page, methods=["GET"]),
        Route("/app/memory/{name}", memory_page, methods=["GET"]),
        Route("/app/memory/{name}/forget", forget_memory, methods=["POST"]),
        Route("/app/review", review_page, methods=["GET"]),
        Route("/app/review/dismiss", dismiss_review, methods=["POST"]),
        Route("/app/export.zip", export_zip, methods=["GET"]),
        Route("/app/docs/{name}", document_page, methods=["GET"]),
    ]
