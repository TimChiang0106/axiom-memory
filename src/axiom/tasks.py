"""Tasks: the Work OS to-do list, kept apart from memories.

The point of the list is that the user can stop asking "what haven't I done
yet": every open task is either due now, planned for a day, or parked as
"someday" — and the few with no arrangement at all are shown so they can be
cleared. Everything is read by date, never by relevance, so tasks do not
enter recall.

AI suggestions are the risky path — an eager model could fill the list with
busywork the user then has to triage. They are rationed here, not in client
prompts: each must name an allowed trigger and cite existing memories, only
a few are accepted per day, look-alikes of open tasks or recent rejections
are refused, and unanswered ones expire.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import asyncpg

from axiom.embeddings import GeminiEmbedder, to_pgvector
from axiom.models import SuggestionTrigger, Task, TaskSource, TaskState, TaskStatus

ALLOWED_TRIGGERS: tuple[SuggestionTrigger, ...] = (
    "unrecorded_commitment",
    "due_soon",
    "overview_stale",
)

_OVERVIEW_SUFFIX = "-overview"

_COLUMNS = (
    "id, title, project, status, source, due_date, planned_date, someday, defer_count, "
    "evidence, reject_reason, done_at, created_at, updated_at, expires_at, "
    "state, waiting_on, phase, code, "
    "COALESCE((SELECT s.waiting FROM task_states s WHERE s.name = state), false) AS waiting"
)

# Fields `update` can clear via its `clear` list.
CLEARABLE = ("project", "due_date", "planned_date", "state", "waiting_on", "phase", "code")

# Free-text limits, matching the column checks.
_TEXT_LIMITS = {"waiting_on": 100, "phase": 40, "code": 20}
_STATE_NAME_LIMIT = 20


class TaskError(ValueError):
    """A refused task operation. `detail` carries structured context for the
    caller (similar tasks, known projects, the quota window)."""

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.detail = detail


def _clean(field_name: str, value: str | None) -> str | None:
    value = (value or "").strip()
    limit = _TEXT_LIMITS[field_name]
    if len(value) > limit:
        raise TaskError(f"{field_name} is longer than {limit} characters.")
    return value or None


@dataclass
class TodayView:
    """What the home page shows: everything that needs a look today."""

    today: date
    overdue: list[Task] = field(default_factory=list)
    due_today: list[Task] = field(default_factory=list)
    unscheduled: list[Task] = field(default_factory=list)
    suggested: list[Task] = field(default_factory=list)
    # Open tasks already arranged for a later day or parked as someday —
    # the "you don't have to think about these" count.
    arranged: int = 0
    # Held by someone else, and not yet the day to follow up.
    waiting: list[Task] = field(default_factory=list)


@dataclass
class WeekView:
    monday: date
    # Seven lists, Monday first: open and done tasks planned on that day.
    days: list[list[Task]]
    done: list[Task]


@dataclass
class ProjectStats:
    project: str
    open: int = 0
    done_this_week: int = 0
    done_total: int = 0
    next_due: date | None = None
    waiting: int = 0
    # The earliest phase that still has open tasks.
    phase: str | None = None
    # Open tasks untouched long enough to ask whether they still matter.
    stale_open: list[Task] = field(default_factory=list)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def later_this_week(today: date) -> list[date]:
    """Days offered by "another day this week": after tomorrow up to Sunday.
    Late in the week there are none left, so next week is offered instead."""
    days = [today + timedelta(days=n) for n in range(2, 7 - today.weekday())]
    if days:
        return days
    monday = week_start(today) + timedelta(days=7)
    tomorrow = today + timedelta(days=1)
    return [d for d in (monday + timedelta(days=n) for n in range(7)) if d > tomorrow]


class TaskStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        embedder: GeminiEmbedder | None = None,
        timezone: str = "UTC",
        suggestion_daily_limit: int = 3,
        suggestion_ttl_days: int = 5,
        rejected_window_days: int = 30,
        similar_trigram: float = 0.5,
        similar_semantic: float = 0.85,
        stale_open_days: int = 14,
    ):
        self._pool = pool
        self._embedder = embedder
        # "Today" is the user's calendar day, not the server's UTC date.
        self._timezone = timezone
        self._suggestion_daily_limit = suggestion_daily_limit
        self._suggestion_ttl_days = suggestion_ttl_days
        self._rejected_window_days = rejected_window_days
        self._similar_trigram = similar_trigram
        self._similar_semantic = similar_semantic
        self.stale_open_days = stale_open_days

    # -- helpers -----------------------------------------------------------

    async def today(self) -> date:
        return await self._pool.fetchval("SELECT (now() AT TIME ZONE $1)::date", self._timezone)

    async def expire_suggestions(self) -> int:
        """Turn suggestions nobody answered in time into 'expired'. Run lazily
        before every read, so no scheduler is needed."""
        result = await self._pool.execute(
            "UPDATE tasks SET status = 'expired', updated_at = now() "
            "WHERE status = 'suggested' AND expires_at <= now()"
        )
        return int(result.split()[-1])

    async def known_projects(self) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT left(name, length(name) - length($1)) AS project FROM memories "
            "WHERE name LIKE '%' || $1 ORDER BY name",
            _OVERVIEW_SUFFIX,
        )
        return [r["project"] for r in rows]

    async def _check_project(self, project: str | None) -> None:
        if project is None:
            return
        known = await self.known_projects()
        if project not in known:
            raise TaskError(
                f"No project overview named '{project}{_OVERVIEW_SUFFIX}'. Use one of the "
                "known projects, or leave project empty.",
                known_projects=known,
            )

    async def phases(self, project: str | None) -> list[str]:
        rows = await self._pool.fetch(
            "SELECT DISTINCT phase FROM tasks WHERE project IS NOT DISTINCT FROM $1 "
            "AND phase IS NOT NULL ORDER BY phase",
            project,
        )
        return [r["phase"] for r in rows]

    async def states(self) -> list[TaskState]:
        rows = await self._pool.fetch(
            "SELECT name, waiting FROM task_states ORDER BY position, name"
        )
        return [TaskState(name=r["name"], waiting=r["waiting"]) for r in rows]

    async def save_state(
        self, name: str, *, waiting: bool = False, rename: str | None = None
    ) -> TaskState:
        """Add a state, or change the one called `rename`; tasks follow the
        new name."""
        name = name.strip()
        if not 1 <= len(name) <= _STATE_NAME_LIMIT:
            raise TaskError(f"A state name is 1 to {_STATE_NAME_LIMIT} characters.")
        try:
            if rename is None:
                await self._pool.execute(
                    "INSERT INTO task_states (name, waiting, position) "
                    "SELECT $1, $2, COALESCE(max(position), 0) + 1 FROM task_states",
                    name,
                    waiting,
                )
            elif (
                await self._pool.fetchval(
                    "UPDATE task_states SET name = $2, waiting = $3 WHERE name = $1 RETURNING name",
                    rename,
                    name,
                    waiting,
                )
                is None
            ):
                raise TaskError(f"No state named '{rename}'.")
        except asyncpg.UniqueViolationError:
            raise TaskError(f"State '{name}' already exists.") from None
        return TaskState(name=name, waiting=waiting)

    async def delete_state(self, name: str) -> None:
        """Finished tasks just lose the word; a live task would silently
        change meaning, so its state has to be moved first."""
        live = await self._pool.fetchval(
            "SELECT count(*) FROM tasks WHERE state = $1 AND status IN ('open', 'suggested')",
            name,
        )
        if live:
            raise TaskError(f"{live} open task(s) are in '{name}'; move them first.")
        await self._pool.execute("DELETE FROM task_states WHERE name = $1", name)

    async def _waiting_on(
        self, state: str | None, waiting_on: str | None, *, kept: str | None = None
    ) -> str | None:
        """Check `state` and work out who the task waits on: that only means
        something while the state waits, so moving out of one drops it."""
        states = await self.states()
        if state is not None and state not in {s.name for s in states}:
            raise TaskError(f"No state named '{state}'.", known_states=[s.name for s in states])
        waits = any(s.waiting and s.name == state for s in states)
        if waiting_on and not waits:
            raise TaskError(
                "waiting_on needs a state that waits.",
                waiting_states=[s.name for s in states if s.waiting],
            )
        return (waiting_on or kept) if waits else None

    async def _embed(self, title: str) -> str | None:
        if self._embedder is None:
            return None
        values = await self._embedder.embed(title, kind="document")
        return to_pgvector(values) if values is not None else None

    async def get(self, task_id: int) -> Task | None:
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM tasks WHERE id = $1", task_id)
        return Task.from_row(row) if row else None

    async def _require(self, task_id: int, *statuses: TaskStatus) -> Task:
        await self.expire_suggestions()
        task = await self.get(task_id)
        if task is None:
            raise TaskError(f"No task with id {task_id}.")
        if statuses and task.status not in statuses:
            raise TaskError(
                f"Task {task_id} is '{task.status}'; this action needs "
                f"{' or '.join(repr(s) for s in statuses)}."
            )
        return task

    # -- writes ------------------------------------------------------------

    async def add(
        self,
        *,
        title: str,
        source: TaskSource = "stated",
        project: str | None = None,
        due_date: date | None = None,
        planned_date: date | None = None,
        someday: bool = False,
        evidence: dict | None = None,
        state: str | None = None,
        waiting_on: str | None = None,
        phase: str | None = None,
        code: str | None = None,
    ) -> Task:
        title = title.strip()
        if not title:
            raise TaskError("Title is empty.")
        if someday and planned_date is not None:
            raise TaskError("A task is either planned for a day or someday, not both.")
        await self._check_project(project)
        waiting_on = await self._waiting_on(state, _clean("waiting_on", waiting_on))
        phase = _clean("phase", phase)
        code = _clean("code", code)
        embedding = await self._embed(title)

        if source == "ai_suggested":
            evidence = await self._screen_suggestion(title, evidence, embedding)
            status: TaskStatus = "suggested"
        else:
            status = "open"

        row = await self._pool.fetchrow(
            f"""
            INSERT INTO tasks (title, project, status, source, due_date, planned_date, someday,
                               evidence, expires_at, embedding, state, waiting_on, phase, code)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb,
                    CASE WHEN $4 = 'ai_suggested'
                         THEN now() + make_interval(days => $9::int) END,
                    $10::vector, $11, $12, $13, $14)
            RETURNING {_COLUMNS}
            """,
            title,
            project,
            status,
            source,
            due_date,
            planned_date,
            someday,
            json.dumps(evidence, ensure_ascii=False) if evidence else None,
            self._suggestion_ttl_days,
            embedding,
            state,
            waiting_on,
            phase,
            code,
        )
        assert row is not None
        return Task.from_row(row)

    async def _screen_suggestion(
        self, title: str, evidence: dict | None, embedding: str | None
    ) -> dict:
        """Rules every AI suggestion must pass. Returns the normalized evidence."""
        evidence = evidence or {}
        trigger = evidence.get("trigger")
        if trigger not in ALLOWED_TRIGGERS:
            raise TaskError(
                f"An AI suggestion needs evidence.trigger in {list(ALLOWED_TRIGGERS)}; "
                f"got {trigger!r}.",
                allowed_triggers=list(ALLOWED_TRIGGERS),
            )
        cited = [n for n in evidence.get("memories") or [] if isinstance(n, str)]
        found = (
            await self._pool.fetch("SELECT name FROM memories WHERE name = ANY($1::text[])", cited)
            if cited
            else []
        )
        existing = {r["name"] for r in found}
        if not existing:
            raise TaskError(
                "An AI suggestion must cite at least one existing memory in "
                "evidence.memories (exact names).",
                missing=cited,
            )

        recent = await self._pool.fetchval(
            "SELECT count(*) FROM tasks WHERE source = 'ai_suggested' "
            "AND created_at > now() - interval '24 hours'"
        )
        if recent >= self._suggestion_daily_limit:
            raise TaskError(
                f"Suggestion quota reached: {recent} AI suggestions in the last 24 hours "
                f"(limit {self._suggestion_daily_limit}). Only suggest what matters most; "
                "try again tomorrow.",
                limit=self._suggestion_daily_limit,
            )

        similar = await self.similar(title, embedding)
        if similar:
            raise TaskError(
                "Too similar to a task that is already open or suggested, or to a "
                f"suggestion rejected in the last {self._rejected_window_days} days.",
                similar=[
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": t.status,
                        "reject_reason": t.reject_reason,
                    }
                    for t in similar
                ],
            )
        note = evidence.get("note")
        return {
            "trigger": trigger,
            "memories": [n for n in cited if n in existing],
            **({"note": str(note)} if note else {}),
        }

    async def similar(self, title: str, embedding: str | None) -> list[Task]:
        """Live tasks and recent rejections a new suggestion would repeat.
        Trigram similarity catches same wording; the embedding, when both
        sides have one, catches paraphrases and cross-language restatements."""
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM (
                SELECT *, GREATEST(
                    similarity(title, $1),
                    CASE WHEN $2::text IS NULL OR embedding IS NULL THEN 0
                         ELSE 1 - (embedding <=> ($2::text)::vector) END
                ) AS score
                FROM tasks
                WHERE status IN ('open', 'suggested')
                   OR rejected_at > now() - make_interval(days => $3::int)
            ) t
            WHERE similarity(title, $1) >= $4
               OR ($2::text IS NOT NULL AND embedding IS NOT NULL
                   AND 1 - (embedding <=> ($2::text)::vector) >= $5)
            ORDER BY score DESC
            LIMIT 3
            """,
            title,
            embedding,
            self._rejected_window_days,
            self._similar_trigram,
            self._similar_semantic,
        )
        return [Task.from_row(r) for r in rows]

    def _pushes_later(
        self, task: Task, *, today: date, planned_date: date | None, someday: bool
    ) -> bool:
        """Whether a new plan moves the task back. Scheduling an unplanned
        task for the first time is planning, not deferring; moving a planned
        (or due-now) task to a later day or to someday is a deferral. Waiting
        longer on someone else is not the user putting it off."""
        if task.waiting:
            return False
        anchor = task.planned_date
        if anchor is None and task.due_date is not None and task.due_date <= today:
            anchor = task.due_date
        if anchor is None:
            return False
        return someday or (planned_date is not None and planned_date > anchor)

    async def update(
        self,
        task_id: int,
        *,
        title: str | None = None,
        project: str | None = None,
        due_date: date | None = None,
        planned_date: date | None = None,
        someday: bool | None = None,
        state: str | None = None,
        waiting_on: str | None = None,
        phase: str | None = None,
        code: str | None = None,
        clear: list[str] | None = None,
    ) -> Task:
        task = await self._require(task_id, "open", "suggested")
        clear = clear or []
        bad = [f for f in clear if f not in CLEARABLE]
        if bad:
            raise TaskError(f"Cannot clear {bad}; clearable fields are {list(CLEARABLE)}.")
        if project is not None:
            await self._check_project(project)

        new_title = title.strip() if title is not None else task.title
        if not new_title:
            raise TaskError("Title is empty.")
        new_project = None if "project" in clear else (project or task.project)
        new_due = None if "due_date" in clear else (due_date or task.due_date)
        new_state = None if "state" in clear else (state or task.state)
        new_waiting_on = await self._waiting_on(
            new_state,
            _clean("waiting_on", waiting_on),
            kept=None if "waiting_on" in clear else task.waiting_on,
        )
        new_phase = None if "phase" in clear else (_clean("phase", phase) or task.phase)
        new_code = None if "code" in clear else (_clean("code", code) or task.code)
        if planned_date is not None:
            new_planned, new_someday = planned_date, False
        elif someday:
            new_planned, new_someday = None, True
        else:
            new_planned = None if "planned_date" in clear else task.planned_date
            new_someday = task.someday if someday is None else False
        today = await self.today()
        bump = (planned_date is not None or someday) and self._pushes_later(
            task, today=today, planned_date=new_planned, someday=new_someday
        )
        embedding = await self._embed(new_title) if new_title != task.title else None
        row = await self._pool.fetchrow(
            f"""
            UPDATE tasks SET title = $2, project = $3, due_date = $4, planned_date = $5,
                   someday = $6, defer_count = defer_count + $7::int,
                   embedding = CASE WHEN $8 THEN $9::vector ELSE embedding END,
                   state = $10, waiting_on = $11, phase = $12, code = $13,
                   updated_at = now()
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            task_id,
            new_title,
            new_project,
            new_due,
            new_planned,
            new_someday,
            1 if bump else 0,
            new_title != task.title,
            embedding,
            new_state,
            new_waiting_on,
            new_phase,
            new_code,
        )
        assert row is not None
        return Task.from_row(row)

    async def defer(self, task_id: int, *, to: Literal["tomorrow", "someday"] | date) -> Task:
        """Re-plan an open task: tomorrow, a given day, or someday."""
        task = await self._require(task_id, "open")
        today = await self.today()
        if to == "someday":
            planned, someday = None, True
        else:
            planned = today + timedelta(days=1) if to == "tomorrow" else to
            if not isinstance(planned, date):
                raise TaskError("defer needs 'tomorrow', 'someday', or a date.")
            if planned < today:
                raise TaskError(f"Cannot defer into the past ({planned} < {today}).")
            someday = False
        bump = self._pushes_later(task, today=today, planned_date=planned, someday=someday)
        row = await self._pool.fetchrow(
            f"""
            UPDATE tasks SET planned_date = $2, someday = $3,
                   defer_count = defer_count + $4::int, updated_at = now()
            WHERE id = $1
            RETURNING {_COLUMNS}
            """,
            task_id,
            planned,
            someday,
            1 if bump else 0,
        )
        assert row is not None
        return Task.from_row(row)

    async def _set_status(self, task_id: int, sql_set: str, *args: object) -> Task:
        row = await self._pool.fetchrow(
            f"UPDATE tasks SET {sql_set}, updated_at = now() WHERE id = $1 RETURNING {_COLUMNS}",
            task_id,
            *args,
        )
        assert row is not None
        return Task.from_row(row)

    async def done(self, task_id: int) -> Task:
        await self._require(task_id, "open", "suggested")
        return await self._set_status(
            task_id, "status = 'done', done_at = now(), expires_at = NULL"
        )

    async def drop(self, task_id: int) -> Task:
        await self._require(task_id, "open", "suggested")
        return await self._set_status(task_id, "status = 'dropped', expires_at = NULL")

    async def reopen(self, task_id: int) -> Task:
        """Undo a done or drop: the task is open again with its plan intact.
        A rejected suggestion stays rejected — that answer feeds the filter."""
        await self._require(task_id, "done", "dropped")
        if await self._pool.fetchval(
            "SELECT rejected_at IS NOT NULL FROM tasks WHERE id = $1", task_id
        ):
            raise TaskError(f"Task {task_id} was a rejected suggestion; it cannot be reopened.")
        return await self._set_status(task_id, "status = 'open', done_at = NULL")

    async def confirm(
        self,
        task_id: int,
        *,
        planned_date: date | None = None,
        due_date: date | None = None,
        someday: bool = False,
    ) -> Task:
        task = await self._require(task_id, "suggested")
        if someday and planned_date is not None:
            raise TaskError("A task is either planned for a day or someday, not both.")
        return await self._set_status(
            task_id,
            "status = 'open', expires_at = NULL, planned_date = $2, due_date = $3, someday = $4",
            planned_date or task.planned_date,
            due_date or task.due_date,
            someday or (task.someday and planned_date is None),
        )

    async def reject(self, task_id: int, *, reason: str | None = None) -> Task:
        await self._require(task_id, "suggested")
        return await self._set_status(
            task_id,
            "status = 'dropped', rejected_at = now(), expires_at = NULL, reject_reason = $2",
            (reason or "").strip() or None,
        )

    # -- reads -------------------------------------------------------------

    async def find(
        self,
        *,
        project: str | None = None,
        statuses: list[TaskStatus] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 200,
    ) -> list[Task]:
        """Filtered list. The date range matches a task planned or due within
        it (inclusive); done tasks also match on the day they were done."""
        await self.expire_suggestions()
        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS} FROM tasks
            WHERE ($1::text IS NULL OR project = $1)
              AND ($2::text[] IS NULL OR status = ANY($2::text[]))
              AND ($3::date IS NULL AND $4::date IS NULL
                   OR planned_date BETWEEN COALESCE($3, '-infinity'::date)
                                       AND COALESCE($4, 'infinity'::date)
                   OR due_date BETWEEN COALESCE($3, '-infinity'::date)
                                   AND COALESCE($4, 'infinity'::date)
                   OR (done_at AT TIME ZONE $6)::date
                      BETWEEN COALESCE($3, '-infinity'::date) AND COALESCE($4, 'infinity'::date))
            ORDER BY COALESCE(planned_date, due_date) NULLS LAST, created_at
            LIMIT $5
            """,
            project,
            statuses,
            date_from,
            date_to,
            limit,
            self._timezone,
        )
        return [Task.from_row(r) for r in rows]

    async def today_view(self) -> TodayView:
        """Partition every open task: overdue, today, unscheduled, or already
        arranged (a later day, or someday). A task re-planned for a later day
        leaves "overdue" even past its deadline — the user decided when. A
        waiting task is never overdue: it stays apart until its follow-up day,
        then comes up today."""
        await self.expire_suggestions()
        today = await self.today()
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM tasks WHERE status IN ('open', 'suggested') "
            "ORDER BY COALESCE(planned_date, due_date) NULLS LAST, created_at"
        )
        view = TodayView(today=today)
        for task in map(Task.from_row, rows):
            if task.status == "suggested":
                view.suggested.append(task)
            elif task.waiting:
                if task.planned_date is not None and task.planned_date <= today:
                    view.due_today.append(task)
                else:
                    view.waiting.append(task)
            elif task.planned_date is not None:
                if task.planned_date < today:
                    view.overdue.append(task)
                elif task.planned_date == today:
                    view.due_today.append(task)
                else:
                    view.arranged += 1
            elif task.due_date is not None and task.due_date < today:
                view.overdue.append(task)
            elif task.due_date == today:
                view.due_today.append(task)
            elif task.someday:
                view.arranged += 1
            else:
                view.unscheduled.append(task)
        return view

    async def done_today(self) -> list[Task]:
        """Tasks finished on the user's calendar day, latest first."""
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM tasks WHERE status = 'done' "
            "AND (done_at AT TIME ZONE $1)::date = (now() AT TIME ZONE $1)::date "
            "ORDER BY done_at DESC",
            self._timezone,
        )
        return [Task.from_row(r) for r in rows]

    async def week_view(self, day: date | None = None) -> WeekView:
        await self.expire_suggestions()
        monday = week_start(day or await self.today())
        sunday = monday + timedelta(days=6)
        planned = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM tasks WHERE status IN ('open', 'done') "
            "AND planned_date BETWEEN $1 AND $2 ORDER BY status DESC, created_at",
            monday,
            sunday,
        )
        days: list[list[Task]] = [[] for _ in range(7)]
        for task in map(Task.from_row, planned):
            assert task.planned_date is not None
            days[(task.planned_date - monday).days].append(task)
        done = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM tasks WHERE status = 'done' "
            "AND (done_at AT TIME ZONE $3)::date BETWEEN $1 AND $2 "
            "ORDER BY project NULLS LAST, done_at",
            monday,
            sunday,
            self._timezone,
        )
        return WeekView(monday=monday, days=days, done=[Task.from_row(r) for r in done])

    async def project_stats(self) -> dict[str | None, ProjectStats]:
        """Per-project counts for the project page, keyed by project (None
        for tasks outside any project)."""
        await self.expire_suggestions()
        today = await self.today()
        monday = week_start(today)
        rows = await self._pool.fetch(
            """
            SELECT project,
                   count(*) FILTER (WHERE status = 'open') AS open,
                   count(*) FILTER (WHERE status = 'done'
                                    AND (done_at AT TIME ZONE $2)::date >= $1) AS done_this_week,
                   count(*) FILTER (WHERE status = 'done') AS done_total,
                   min(due_date) FILTER (WHERE status = 'open') AS next_due,
                   count(*) FILTER (WHERE status = 'open' AND state IN (
                       SELECT name FROM task_states WHERE waiting)) AS waiting,
                   min(phase) FILTER (WHERE status = 'open') AS phase
            FROM tasks GROUP BY project
            """,
            monday,
            self._timezone,
        )
        stats = {
            r["project"]: ProjectStats(
                project=r["project"] or "",
                open=r["open"],
                done_this_week=r["done_this_week"],
                done_total=r["done_total"],
                next_due=r["next_due"],
                waiting=r["waiting"],
                phase=r["phase"],
            )
            for r in rows
        }
        stale = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM tasks WHERE status = 'open' "
            "AND updated_at < now() - make_interval(days => $1::int) ORDER BY updated_at",
            self.stale_open_days,
        )
        for task in map(Task.from_row, stale):
            stats[task.project].stale_open.append(task)
        return stats
