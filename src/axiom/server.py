"""MCP surface: the memory tools over the store.

Tool docstrings double as prompts for the calling model — they carry the write
discipline (one curated fact per memory, no conversation logs) so behavior does
not depend on each client's custom instructions alone.
"""

import asyncio
from datetime import date
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import Field

from axiom.auth import AppAuth, build_auth
from axiom.config import get_settings
from axiom.db import create_pool, run_migrations
from axiom.documents import DocumentStore
from axiom.embeddings import GeminiEmbedder
from axiom.models import (
    NAME_PATTERN,
    Category,
    MemorySummary,
    MemoryType,
    MemoryVersion,
    Provenance,
    RememberResult,
    ReviewReport,
)
from axiom.store import MemoryStore
from axiom.tasks import TaskError, TaskStore
from axiom.web import build_routes

_store: MemoryStore | None = None
_tasks: TaskStore | None = None
_documents: DocumentStore | None = None
_store_lock = asyncio.Lock()


async def _init() -> None:
    """Open the pool, migrate, and build every store over it — once."""
    global _store, _tasks, _documents
    async with _store_lock:
        if _store is not None:
            return
        settings = get_settings()
        pool = await create_pool(settings.database_url)
        await run_migrations(pool)
        embedder = None
        if settings.gemini_api_key:
            embedder = GeminiEmbedder(
                api_key=settings.gemini_api_key,
                model=settings.embedding_model,
                dims=settings.embedding_dims,
                query_timeout_seconds=settings.embedding_query_timeout_seconds,
            )
        _tasks = TaskStore(
            pool,
            embedder=embedder,
            timezone=settings.timezone,
            suggestion_daily_limit=settings.suggestion_daily_limit,
            suggestion_ttl_days=settings.suggestion_ttl_days,
            rejected_window_days=settings.rejected_window_days,
            similar_trigram=settings.task_similar_trigram,
            similar_semantic=settings.task_similar_semantic,
            stale_open_days=settings.stale_open_days,
        )
        # Suggestions that lapsed while the server was down expire at startup.
        await _tasks.expire_suggestions()
        _documents = DocumentStore(pool)
        _store = MemoryStore(
            pool,
            dedup_threshold=settings.dedup_threshold,
            semantic_dedup_threshold=settings.semantic_dedup_threshold,
            recall_threshold=settings.recall_threshold,
            recall_half_life_days=settings.recall_half_life_days,
            state_half_life_days=settings.state_half_life_days,
            procedural_half_life_days=settings.procedural_half_life_days,
            use_count_dampening=settings.use_count_dampening,
            lexical_weight=settings.lexical_weight,
            inferred_weight=settings.inferred_weight,
            stale_state_days=settings.stale_state_days,
            event_similarity=settings.event_similarity,
            event_retention_days=settings.event_retention_days,
            embedder=embedder,
            semantic_floor=settings.semantic_floor,
            write_neighbor_threshold=settings.write_neighbor_threshold,
            write_neighbor_count=settings.write_neighbor_count,
        )


async def get_store() -> MemoryStore:
    if _store is None:
        await _init()
    assert _store is not None
    return _store


async def get_tasks() -> TaskStore:
    if _tasks is None:
        await _init()
    assert _tasks is not None
    return _tasks


async def get_documents() -> DocumentStore:
    if _documents is None:
        await _init()
    assert _documents is not None
    return _documents


def _client_name(ctx: Context) -> str:
    # Stateful transports (stdio) carry the MCP initialize clientInfo.
    try:
        client_info = ctx.session.client_params.clientInfo  # type: ignore[union-attr]
        if client_info.name:
            return client_info.name
    except AttributeError:
        pass
    # Stateless HTTP gets a fresh session per request; fall back to User-Agent.
    try:
        request = get_http_request()
        agent = request.headers.get("user-agent", "")
        if agent:
            return agent.split("/")[0].split(" ")[0].lower()
    except RuntimeError:
        pass
    return "unknown"


mcp = FastMCP(
    name="axiom",
    instructions=(
        "Axiom is the user's personal long-term memory, shared across all their "
        "AI apps. At the start of a substantial conversation, `get` the memory named "
        "`user-profile` (who the user is, how they think, how to work with them) and "
        "keep it in mind throughout. The user reads `user-profile` as their About-me "
        "page: when they ask you to organize or refresh it, `list_memories` with "
        "category 'you', `get` the ones that matter, and rewrite `user-profile` in "
        "their language as `## ` sections with `- ` bullets, `related` listing the "
        "memories it draws on. Memories are organized by lifecycle `type` (preference, fact, "
        "project, state, reference, procedural) and thematic `category` (you, people, "
        "areas, topics). Call `recall` before answering anything that could depend on "
        "their preferences, ongoing projects, contacts, or past decisions. Call `remember` "
        "when the user states a durable fact, preference, or decision — store one "
        "curated fact per memory, never conversation logs. Call `log_event` for "
        "weak ambient signals (what they asked about or did) that aren't worth a "
        "curated memory yet. Each ongoing project keeps one `<project>-overview` "
        "memory (status with a date, decisions in force, abandoned approaches, open "
        "items, key detail memories in `related`): `get` it first when asked where a "
        "project stands, and update it after a substantial session on that project. "
        "Overviews hold no to-do lists: when the user mentions something they need "
        "to do or a deadline, `task` add it (source 'stated', with planned_date if "
        "they said which day); when they say they finished or sent one, `task` list "
        "to find it and mark it done. Long documents (specs, plans, notes) go in `document`, "
        "not in a memory; a memory can point at one with `doc:<name>`. "
        "Give context-bound memories (a job's templates, a project's specifics) the "
        "overview's name prefix and link them from it, so forgetting the overview "
        "retires them as a group; keep portable know-how in `topics` without the prefix."
    ),
    auth=build_auth(get_settings()),
)


@mcp.tool
async def remember(
    ctx: Context,
    name: Annotated[
        str,
        Field(
            pattern=NAME_PATTERN,
            description="Stable kebab-case ASCII slug, e.g. 'prefers-uv-over-pip'.",
        ),
    ],
    description: Annotated[
        str, Field(description="One-line summary used to judge relevance during recall.")
    ],
    content: Annotated[
        str,
        Field(
            description=(
                "The fact itself, in markdown. One curated statement — distill it; "
                "do not paste conversation logs. Write dates as absolute dates."
            )
        ),
    ],
    type: Annotated[
        MemoryType,
        Field(
            description=(
                "preference: durable taste or working style. fact: durable personal fact. "
                "project: ongoing work context. state: current setup that will go stale "
                "(tools, versions). reference: pointer to an external resource. "
                "procedural: how-to workflow or behavioural rule — write the content as "
                "a trigger condition (**When:**), the steps (**Do:**), and a one-line "
                "reason (**Why:**)."
            )
        ),
    ],
    category: Annotated[
        Category | None,
        Field(
            description=(
                "Thematic category matching Claude's memory model: "
                "'you' (user profile, communication preferences, personal habits), "
                "'people' (colleagues, clients, collaborators, relationships), "
                "'areas' (long-term projects, products, companies, ventures), "
                "'topics' (domain expertise, workflows, guidelines, investing, technical stacks). "
                "Optional; if omitted, automatically inferred from type."
            )
        ),
    ] = None,
    related: Annotated[
        list[str],
        Field(
            description=(
                "Names of related memories to link to, so recall can point onward "
                "to them. Optional; use the exact names of existing memories."
            )
        ),
    ] = [],  # noqa: B006 — FastMCP reads the default to build the schema.
    provenance: Annotated[
        Provenance | None,
        Field(
            description=(
                "'stated' if the user said this directly; 'inferred' if you concluded "
                "it from their behaviour, choices, or other indirect evidence (e.g. "
                "they keep asking about X, so they probably care about X). Inferred "
                "memories rank lower and are flagged so they get confirmed before "
                "anyone relies on them. When the user confirms an inferred memory, "
                "re-remember it with 'stated'. Omit on update to keep the current value."
            )
        ),
    ] = None,
    allow_duplicate: Annotated[
        bool,
        Field(
            description=(
                "Only set True after a duplicate_suspected response, and only if the "
                "suspected matches are genuinely different memories."
            )
        ),
    ] = False,
) -> RememberResult:
    """Save or update one memory.

    If a memory with the same name exists it is updated in place. If a new
    memory looks similar to existing ones, this returns duplicate_suspected
    with the matches instead of saving — update the existing memory by reusing
    its name, or retry with allow_duplicate=True if it is genuinely new.

    On a successful create the result also carries `neighbors`: the nearest
    existing memories that were below the duplicate bar. These are not
    duplicates, but check them — if the new fact revises or contradicts one
    (e.g. a project overview or plan it supersedes), update that memory by its
    name instead of leaving two records that disagree.
    """
    store = await get_store()
    return await store.remember(
        name=name,
        description=description,
        content=content,
        type=type,
        category=category,
        source_app=_client_name(ctx),
        related=related,
        provenance=provenance,
        allow_duplicate=allow_duplicate,
    )


@mcp.tool
async def get(
    name: Annotated[str, Field(description="Exact name of the memory.")],
) -> dict:
    """Fetch one memory in full by its exact name — for a recall result
    flagged truncated, or a name seen in `related` or the index."""
    store = await get_store()
    memory = await store.get(name)
    if memory is None:
        return {"found": False, "name": name}
    return {"found": True, "memory": memory.payload()}


@mcp.tool
async def recall(
    query: Annotated[
        str,
        Field(
            description=(
                "Search terms: topics, project names, tools, or aspects of the user. "
                "Prefer a few distinct keywords over full sentences."
            )
        ),
    ],
    category: Annotated[
        Category | None,
        Field(
            description=(
                "Optional filter by category: 'you', 'people', 'areas', or 'topics'. "
                "Useful when searching specifically for people/contacts or project context."
            )
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
) -> dict:
    """Search the user's long-term memory.

    Call this before answering anything that could depend on the user's
    preferences, projects, or past decisions. A result with possibly_stale=true
    is either a `state` memory that has not been updated in a while, or a
    memory (typically a project overview) whose linked details were updated
    more recently than it was — so it may be behind. Verify it with the user
    before relying on it, and re-`remember` it under the same name once
    confirmed so it stops being flagged. A result with
    provenance='inferred' is an AI's conclusion, not something the user
    said — treat it as a hypothesis and confirm it before acting on it or
    presenting it as fact. A result with truncated=true
    carries only the start of its content — call `get` with its name when
    the rest matters. `related` lists memories linked from the matches —
    `get` one by its name if it looks relevant. On a miss,
    the nearest index entries (names and descriptions) are returned instead —
    scan them and re-query with matching keywords rather than concluding
    nothing is known.
    """
    store = await get_store()
    memories = await store.recall(query, category=category, limit=limit)
    max_chars = get_settings().recall_content_chars
    if not memories:
        nearest = await store.index_nearest(query, category=category)
        return {
            "matches": [],
            "note": (
                "No direct hits. Below are the nearest index entries — if one looks "
                "relevant, call recall again with keywords from its name or description."
            ),
            "index": [
                {"name": m.name, "description": m.description, "category": m.category}
                for m in nearest
            ],
        }
    # Expand `related` one hop: link targets the matches point at but that
    # did not surface themselves, as index entries the caller can pull next.
    hit_names = {m.name for m in memories}
    linked = list(dict.fromkeys(n for m in memories for n in m.related if n not in hit_names))
    result: dict = {"matches": [m.payload(max_chars) for m in memories]}
    related = await store.summaries_by_names(linked)
    if related:
        result["related"] = [
            {"name": r.name, "description": r.description, "category": r.category} for r in related
        ]
    return result


@mcp.tool
async def log_event(
    ctx: Context,
    content: Annotated[
        str,
        Field(
            max_length=500,
            description=(
                "One short line describing what happened, e.g. 'asked for a miso "
                "soup recipe' or 'discussed Gemini Enterprise architecture for a "
                "customer'. Absolute wording, no conversation excerpts."
            ),
        ),
    ],
) -> dict:
    """Log one ambient event — a weak signal not worth a curated memory.

    Use this liberally for what the user did or asked about (topics, meetings,
    activities); it is append-only and cheap, and never pollutes recall.
    Recurring patterns are surfaced later for promotion to a real memory.
    For a durable fact, preference, or decision, use `remember` instead.
    """
    store = await get_store()
    event = await store.log_event(content=content, source_app=_client_name(ctx))
    return {"logged": True, "id": event.id}


@mcp.tool
async def forget(
    name: Annotated[str, Field(description="Exact name of the memory to delete.")],
) -> dict:
    """Delete one memory by name. Use when the user asks to forget something
    or a memory is confirmed obsolete. Its final state stays in `history`,
    so a mistaken forget can be undone.

    Forgetting a `<project>-overview` retires that project (or job): the
    result's `left_behind` lists the memories that belonged to it and still
    surface in recall. Walk them with the user — forget the ones tied to that
    context, and re-home anything worth keeping (e.g. a general method as a
    `topics` memory without the project prefix)."""
    store = await get_store()
    deleted = await store.forget(name)
    result: dict = {"deleted": deleted, "name": name}
    if deleted and name.endswith("-overview"):
        result["left_behind"] = [m.name for m in await store.left_behind(name)]
    return result


@mcp.tool
async def history(
    name: Annotated[str, Field(description="Exact name of the memory.")],
) -> list[MemoryVersion]:
    """List past versions of one memory, newest first — what it said before
    each update, and its final state if it was forgotten.

    Use when the user asks what a memory used to say, or when an update or
    forget looks like a mistake. To restore a version, call `remember` with
    the same name and that version's description, content, type, and
    category; only do so after the user confirms.
    """
    store = await get_store()
    return await store.history(name)


@mcp.tool
async def review() -> ReviewReport:
    """Report consolidation candidates across the whole store: near-duplicate
    pairs worth merging, stale `state` memories to re-verify, never-recalled
    memories that may not be worth keeping, project overviews that have fallen
    behind their own detail memories (refresh them), project notes orphaned
    from their overview (fold them in), memories left behind by a forgotten
    overview (retire or re-home them), and recurring event clusters that may
    deserve promotion to a real memory.

    Use this to run a reflection session: walk the report with the user,
    merge or refresh via `remember`, and confirm before any `forget`. When
    writing back a higher-level insight synthesized from several memories,
    always cite the sources by listing their names in `related`.
    """
    settings = get_settings()
    store = await get_store()
    return await store.consolidation_candidates(
        stale_state_days=settings.stale_state_days,
        zombie_days=settings.zombie_days,
        zombie_max_use_count=settings.zombie_max_use_count,
        event_window_days=settings.event_window_days,
        event_min_count=settings.event_min_count,
    )


@mcp.tool
async def list_memories(
    category: Annotated[
        Category | None,
        Field(description=("Optional category filter: 'you', 'people', 'areas', or 'topics'.")),
    ] = None,
) -> list[MemorySummary]:
    """List every memory (name, one-line description, type, category, last update) without
    bodies. Use to browse what is known, or when recall misses and you want to
    scan the index directly."""
    store = await get_store()
    return await store.list_all(category=category)


def _task_payload(task) -> dict:
    return task.model_dump(mode="json", exclude_none=True)


@mcp.tool
async def task(
    action: Annotated[
        Literal["add", "update", "done", "drop", "confirm", "reject", "defer", "list", "states"],
        Field(description="What to do; see the tool description."),
    ],
    id: Annotated[
        int | None,
        Field(description="Task id — required for every action except add and list."),
    ] = None,
    title: Annotated[
        str | None, Field(max_length=300, description="Short imperative title.")
    ] = None,
    project: Annotated[
        str | None,
        Field(
            description=(
                "The project's overview name prefix (e.g. 'axiom' for axiom-overview). "
                "Must match an existing overview; omit for tasks outside any project. "
                "On list: filter by project."
            )
        ),
    ] = None,
    source: Annotated[
        Literal["stated", "ai_suggested"],
        Field(
            description=(
                "add only. 'stated': the user said it (opens immediately). "
                "'ai_suggested': your own proposal — waits for the user to confirm."
            )
        ),
    ] = "stated",
    due_date: Annotated[date | None, Field(description="Deadline, YYYY-MM-DD.")] = None,
    planned_date: Annotated[
        date | None, Field(description="The day the user plans to do it, YYYY-MM-DD.")
    ] = None,
    someday: Annotated[
        bool | None, Field(description="Parked for later with no date (add/update/confirm).")
    ] = None,
    evidence: Annotated[
        dict | None,
        Field(
            description=(
                'Why this task exists: {"trigger": ..., "memories": [exact memory '
                'names], "note": optional}. Required for ai_suggested, where trigger '
                "must be unrecorded_commitment, due_soon, or overview_stale and at "
                "least one cited memory must exist."
            )
        ),
    ] = None,
    to: Annotated[
        str | None,
        Field(description="defer only: 'tomorrow', 'someday', or a date YYYY-MM-DD."),
    ] = None,
    reason: Annotated[
        str | None, Field(description="reject only: why, in the user's words.")
    ] = None,
    clear: Annotated[
        list[str] | None,
        Field(
            description=(
                "update only: fields to blank out — project, due_date, planned_date, "
                "state, waiting_on, phase, code."
            )
        ),
    ] = None,
    state: Annotated[
        str | None,
        Field(
            description=(
                "add/update: where the task stands, one of the names from action "
                "'states'. Optional; leave it out of ordinary to-dos."
            )
        ),
    ] = None,
    waiting_on: Annotated[
        str | None,
        Field(
            max_length=100,
            description="add/update: who or what it waits on; needs a state that waits.",
        ),
    ] = None,
    phase: Annotated[
        str | None,
        Field(
            max_length=40,
            description=(
                "add/update: delivery phase, e.g. '07 測試與安全驗證'. Reuse the exact "
                "name other tasks in the project use."
            ),
        ),
    ] = None,
    code: Annotated[
        str | None,
        Field(max_length=20, description="add/update: WBS number, e.g. 'AG-6.2'."),
    ] = None,
    status: Annotated[
        list[Literal["suggested", "open", "done", "dropped", "expired"]] | None,
        Field(description="list only: filter by status."),
    ] = None,
    date_from: Annotated[
        date | None, Field(description="list only: planned, due, or done on/after this day.")
    ] = None,
    date_to: Annotated[
        date | None, Field(description="list only: planned, due, or done on/before this day.")
    ] = None,
) -> dict:
    """The user's to-do list (their work home page reads it). Use this — not
    memories, not project overviews — for anything they need to do.

    - add: whenever the user says they need to, will, or should do something,
      or mentions a deadline, add it right away with source='stated'. If they
      said which day they'll do it, set planned_date; a deadline goes in
      due_date; "later / no rush" is someday=true. One task per action item.
    - add with source='ai_suggested': only for something the user did not ask
      for but plainly needs — a commitment they made but never recorded
      (unrecorded_commitment), a deadline coming up (due_soon), or a project
      overview that has fallen behind (overview_stale). Cite the memories it
      comes from in evidence. The server allows a few per day and refuses
      look-alikes of open tasks or recently rejected ones; a refusal is final
      for today — do not rephrase and retry. Unanswered suggestions expire
      after 5 days.
    - update: change title / project / dates / state / phase (pass `clear` to
      blank a field).
    - states: the state names the user keeps. When a task is held by someone
      else, set a state with waiting=true plus waiting_on; it then cannot go
      overdue, and planned_date is the day to follow up.
    - done / drop: finished, or no longer wanted.
    - confirm / reject: the user's answer to a suggestion (reject with their
      reason, so the same idea is not proposed again).
    - defer: move an open task to 'tomorrow', a date, or 'someday'.
    - list: with no filters, what needs a look today — overdue, planned or due
      today, unscheduled (open with no day and not someday — aim to give each
      a day), and suggestions awaiting an answer — plus what is waiting on
      someone else and how many open tasks are already arranged for later.
      With project / status / date filters, a flat list.
    """
    tasks = await get_tasks()
    try:
        if action == "states":
            return {"states": [s.model_dump() for s in await tasks.states()]}
        if action == "list":
            if project or status or date_from or date_to:
                found = await tasks.find(
                    project=project, statuses=status, date_from=date_from, date_to=date_to
                )
                return {"tasks": [_task_payload(t) for t in found]}
            view = await tasks.today_view()
            return {
                "today": view.today.isoformat(),
                "overdue": [_task_payload(t) for t in view.overdue],
                "due_today": [_task_payload(t) for t in view.due_today],
                "unscheduled": [_task_payload(t) for t in view.unscheduled],
                "suggested": [_task_payload(t) for t in view.suggested],
                "waiting": [_task_payload(t) for t in view.waiting],
                "arranged_for_later": view.arranged,
            }
        if action == "add":
            if not title:
                raise TaskError("add needs a title.")
            created = await tasks.add(
                title=title,
                source=source,
                project=project,
                due_date=due_date,
                planned_date=planned_date,
                someday=bool(someday),
                evidence=evidence,
                state=state,
                waiting_on=waiting_on,
                phase=phase,
                code=code,
            )
            return {"status": created.status, "task": _task_payload(created)}
        if id is None:
            raise TaskError(f"{action} needs the task id.")
        if action == "update":
            result = await tasks.update(
                id,
                title=title,
                project=project,
                due_date=due_date,
                planned_date=planned_date,
                someday=someday,
                state=state,
                waiting_on=waiting_on,
                phase=phase,
                code=code,
                clear=clear,
            )
        elif action == "done":
            result = await tasks.done(id)
        elif action == "drop":
            result = await tasks.drop(id)
        elif action == "confirm":
            result = await tasks.confirm(
                id, planned_date=planned_date, due_date=due_date, someday=bool(someday)
            )
        elif action == "reject":
            result = await tasks.reject(id, reason=reason)
        else:  # defer
            if not to:
                raise TaskError("defer needs `to`: 'tomorrow', 'someday', or YYYY-MM-DD.")
            target = to if to in ("tomorrow", "someday") else date.fromisoformat(to)
            result = await tasks.defer(id, to=target)  # type: ignore[arg-type]
        return {"status": result.status, "task": _task_payload(result)}
    except TaskError as e:
        return {"error": str(e), **e.detail}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool
async def document(
    ctx: Context,
    action: Annotated[Literal["put", "get", "list"], Field(description="put, get, or list.")],
    name: Annotated[
        str | None,
        Field(
            pattern=r"^(doc:)?[a-z0-9][a-z0-9-]{1,63}$",
            description=(
                "Stable kebab-case slug (put/get), e.g. 'axiom-work-os-spec'; a "
                "`doc:` prefix from a memory reference is accepted."
            ),
        ),
    ] = None,
    title: Annotated[str | None, Field(max_length=300, description="put: document title.")] = None,
    body: Annotated[str | None, Field(description="put: the full text, markdown.")] = None,
    project: Annotated[
        str | None,
        Field(description="Project overview name prefix it belongs to (put), or filter (list)."),
    ] = None,
    history: Annotated[bool, Field(description="get: also return past versions.")] = False,
) -> dict:
    """Long-form documents — specs, plans, meeting notes, drafts: anything too
    long for a memory. Not searchable through recall; fetch by name.

    - put: create or overwrite by name (the previous version is kept). Write
      the whole body each time. Then, if a memory should lead to it, mention
      `doc:<name>` in that memory's content.
    - get: the full document by name (a `doc:<name>` reference in a memory
      points here); history=true adds earlier versions.
    - list: names, titles, and sizes, optionally for one project.
    """
    docs = await get_documents()
    if action == "list":
        return {
            "documents": [d.model_dump(mode="json") for d in await docs.list_all(project=project)]
        }
    if not name:
        return {"error": f"{action} needs a name."}
    name = name.removeprefix("doc:")
    if action == "get":
        doc = await docs.get(name)
        if doc is None:
            return {"found": False, "name": name}
        result: dict = {"found": True, "document": doc.model_dump(mode="json")}
        if history:
            result["versions"] = [v.model_dump(mode="json") for v in await docs.history(doc.name)]
        return result
    if not title or body is None:
        return {"error": "put needs title and body."}
    if project is not None and project not in await (await get_tasks()).known_projects():
        return {"error": f"No project overview named '{project}-overview'."}
    status, doc = await docs.put(
        name=name, title=title, body=body, project=project, source_app=_client_name(ctx)
    )
    return {"status": status, "name": doc.name, "chars": len(doc.body)}


for _route in build_routes(
    auth=AppAuth.from_settings(get_settings()),
    get_tasks=get_tasks,
    get_memories=get_store,
    get_documents=get_documents,
):
    mcp.custom_route(_route.path, methods=sorted(_route.methods or []))(_route.endpoint)  # type: ignore[arg-type]
