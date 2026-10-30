"""Memory pages: the library and search, a single memory or document, the
review inbox, the "我" page, and the backup download."""

import io
import zipfile
from collections import Counter
from datetime import datetime
from html import escape

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from axiom.export import render_index, render_memory
from axiom.web import components as ui
from axiom.web.context import WebContext
from axiom.web.labels import (
    CATEGORY_LABELS,
    PROFILE,
    TYPE_LABELS,
)
from axiom.web.layout import page


def routes(ctx: WebContext) -> list[Route]:
    async def library_page(request: Request) -> Response:
        if (denied := await ctx.guard(request)) is not None:
            return denied
        memories = await ctx.get_memories()
        documents = await ctx.get_documents()
        q = request.query_params.get("q", "").strip()
        body = (
            "<h1>資料</h1>"
            '<form class="search" method="get" action="/app/library">'
            f'<input type="search" name="q" value="{escape(q)}" placeholder="搜尋記憶" '
            'aria-label="搜尋記憶" enterkeyhint="search">'
            '<button class="primary">搜尋</button></form>'
        )
        body += await ctx.notices(request, await ctx.get_tasks())

        if q:
            found = await memories.recall(q, limit=20, track_usage=False)
            body += ui.section(
                "搜尋結果",
                [ui.memory_row(m.name, m.description, m.type) for m in found],
                empty="沒有符合的記憶。",
                grid=True,
            )
            return page(f"搜尋 {q}", "library", body)
        category = request.query_params.get("c", "")
        if category not in CATEGORY_LABELS:
            category = ""
        everything = await memories.list_all()
        counts = Counter(m.category for m in everything)
        chips = [("", "全部", len(everything))] + [
            (key, label, counts[key]) for key, label in CATEGORY_LABELS.items() if counts[key]
        ]
        body += (
            '<div class="chips" role="navigation" aria-label="記憶分類">'
            + "".join(
                f'<a href="{ui.with_query("/app/library", c=key) if key else "/app/library"}"'
                f"{' class=on aria-current=page' if key == category else ''}>"
                f"{label}<span>{n}</span></a>"
                for key, label, n in chips
            )
            + "</div>"
        )
        if category:
            body += ui.section(
                CATEGORY_LABELS[category],
                [
                    ui.memory_row(m.name, m.description, m.type)
                    for m in everything
                    if m.category == category
                ],
                grid=True,
            )
            return page(CATEGORY_LABELS[category], "library", body)
        body += (
            '<div class="list add"><a class="row" href="/app/review">'
            '<div class="title">整理記憶 ›</div>'
            '<div class="meta">重複、過時、沒連到總覽的記憶，和 AI 最近記下的動態</div></a></div>'
        )
        docs = await documents.list_all()
        body += ui.section(
            "文件",
            [
                f'<a class="row" href="/app/docs/{escape(d.name)}"><div class="title">'
                f'{escape(d.title)}</div><div class="meta">'
                f"{escape(d.project) + '・' if d.project else ''}"
                f"{d.chars} 字・{d.updated_at:%Y-%m-%d} 更新</div></a>"
                for d in docs
            ],
            empty="還沒有文件。",
            grid=True,
        )
        body += ui.section(
            "所有記憶・最近更新在前",
            [ui.memory_row(m.name, m.description, m.type) for m in everything],
            grid=True,
        )
        return page("資料", "library", body)

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
            groups.setdefault(m.type, []).append(ui.memory_row(m.name, m.description))
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
            f"{back}<h1>{escape(memory.name)}</h1>"
            f'<div class="meta">{escape(memory.description)}</div>'
            f'<div class="tags"><span class="tag">{TYPE_LABELS[memory.type]}</span>'
            f'<span class="tag">{CATEGORY_LABELS[memory.category]}</span>'
            f"<span>{said}・{ui.app(memory.source_app)} 最後寫入</span></div>"
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
            [ui.memory_row(m.name, m.description) for m in await memories.backlinks(memory.name)],
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
        return page(memory.name, "library", body)

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
