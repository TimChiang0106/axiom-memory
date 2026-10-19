"""Tasks: the suggestion guard rails (a-d), expiry, deferral counting, and
the today-view partition the home page is built on."""

from datetime import timedelta

import asyncpg
import pytest
from test_semantic import FakeEmbedder, unit

from axiom.store import MemoryStore
from axiom.tasks import TaskError, TaskStore, later_this_week

EVIDENCE = {"trigger": "due_soon", "memories": ["axiom-overview"]}


async def _overview(pool: asyncpg.Pool, name: str = "axiom-overview") -> None:
    store = MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)
    await store.remember(
        name=name,
        description=f"{name} project overview",
        content="Status as of 2026-10-19.",
        type="project",
        source_app="test",
        allow_duplicate=True,
    )


async def test_stated_task_opens_immediately(tasks: TaskStore):
    task = await tasks.add(title="Write the deploy notes")
    assert task.status == "open"
    assert task.source == "stated"
    assert task.expires_at is None


async def test_project_must_have_an_overview(tasks: TaskStore, pool: asyncpg.Pool):
    with pytest.raises(TaskError) as err:
        await tasks.add(title="Ship it", project="axiom")
    assert err.value.detail["known_projects"] == []

    await _overview(pool)
    task = await tasks.add(title="Ship it", project="axiom")
    assert task.project == "axiom"


# -- a. trigger and evidence ---------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        {"memories": ["axiom-overview"]},
        {"trigger": "felt_like_it", "memories": ["axiom-overview"]},
    ],
)
async def test_suggestion_needs_an_allowed_trigger(
    tasks: TaskStore, pool: asyncpg.Pool, evidence: dict | None
):
    await _overview(pool)
    with pytest.raises(TaskError, match="trigger"):
        await tasks.add(title="Refresh the overview", source="ai_suggested", evidence=evidence)


@pytest.mark.parametrize("memories", [[], ["no-such-memory"]])
async def test_suggestion_must_cite_an_existing_memory(
    tasks: TaskStore, pool: asyncpg.Pool, memories: list[str]
):
    await _overview(pool)
    with pytest.raises(TaskError, match="existing memory"):
        await tasks.add(
            title="Refresh the overview",
            source="ai_suggested",
            evidence={"trigger": "overview_stale", "memories": memories},
        )


async def test_valid_suggestion_waits_and_drops_unknown_citations(
    tasks: TaskStore, pool: asyncpg.Pool
):
    await _overview(pool)
    task = await tasks.add(
        title="Refresh the overview",
        source="ai_suggested",
        evidence={"trigger": "overview_stale", "memories": ["axiom-overview", "ghost"]},
    )
    assert task.status == "suggested"
    assert task.evidence is not None
    assert task.evidence.memories == ["axiom-overview"]
    assert task.expires_at is not None
    lifetime = task.expires_at - task.created_at
    assert timedelta(days=4, hours=23) < lifetime <= timedelta(days=5)


# -- b. daily quota ------------------------------------------------------------


async def test_at_most_three_suggestions_per_24_hours(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    titles = ["Renew the domain", "Book dentist appointment", "Send invoice to client"]
    for title in titles:
        await tasks.add(title=title, source="ai_suggested", evidence=EVIDENCE)
    with pytest.raises(TaskError, match="quota") as err:
        await tasks.add(title="Water the plants", source="ai_suggested", evidence=EVIDENCE)
    assert err.value.detail["limit"] == 3

    # Answering a suggestion does not refund it; the window is what counts.
    first = (await tasks.today_view()).suggested[0]
    await tasks.reject(first.id, reason="no")
    with pytest.raises(TaskError, match="quota"):
        await tasks.add(title="Water the plants", source="ai_suggested", evidence=EVIDENCE)

    await pool.execute("UPDATE tasks SET created_at = now() - interval '25 hours'")
    task = await tasks.add(title="Water the plants", source="ai_suggested", evidence=EVIDENCE)
    assert task.status == "suggested"


async def test_stated_tasks_are_not_rationed(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    for i in range(5):
        await tasks.add(title=f"Stated thing number {i} unrelated {i * 7}")
    assert await tasks.add(title="Water plants", source="ai_suggested", evidence=EVIDENCE)


# -- c. look-alikes ------------------------------------------------------------


async def test_suggestion_like_an_open_task_is_refused(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    open_task = await tasks.add(title="Update the Axiom README")
    with pytest.raises(TaskError, match="similar") as err:
        await tasks.add(
            title="Update the Axiom README file", source="ai_suggested", evidence=EVIDENCE
        )
    assert err.value.detail["similar"][0]["id"] == open_task.id


async def test_suggestion_like_a_recent_rejection_is_refused(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    first = await tasks.add(
        title="Migrate backups to Cloud Storage", source="ai_suggested", evidence=EVIDENCE
    )
    await tasks.reject(first.id, reason="already done by cron")
    with pytest.raises(TaskError, match="similar") as err:
        await tasks.add(
            title="Migrate backups to Cloud Storage now", source="ai_suggested", evidence=EVIDENCE
        )
    assert err.value.detail["similar"][0]["reject_reason"] == "already done by cron"

    # Past the 30-day window the rejection no longer blocks.
    await pool.execute("UPDATE tasks SET rejected_at = now() - interval '31 days'")
    again = await tasks.add(
        title="Migrate backups to Cloud Storage now", source="ai_suggested", evidence=EVIDENCE
    )
    assert again.status == "suggested"


async def test_done_and_dropped_tasks_do_not_block(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    done = await tasks.add(title="Rotate the service token")
    await tasks.done(done.id)
    dropped = await tasks.add(title="Rotate the Gemini key")
    await tasks.drop(dropped.id)
    assert await tasks.add(
        title="Rotate the service token", source="ai_suggested", evidence=EVIDENCE
    )


async def test_semantic_look_alike_is_refused(pool: asyncpg.Pool):
    await pool.execute("TRUNCATE memories, memory_versions, tasks RESTART IDENTITY")
    await _overview(pool)
    embedder = FakeEmbedder(
        {"續約網域": unit((0, 1.0)), "Renew domain": unit((0, 0.95), (1, 0.312))}
    )
    store = TaskStore(pool, embedder=embedder)  # type: ignore[arg-type]
    await store.add(title="續約網域")
    with pytest.raises(TaskError, match="similar"):
        await store.add(title="Renew domain", source="ai_suggested", evidence=EVIDENCE)


# -- d. expiry -----------------------------------------------------------------


async def test_unanswered_suggestion_expires(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    task = await tasks.add(title="Renew the domain", source="ai_suggested", evidence=EVIDENCE)
    assert [t.id for t in (await tasks.today_view()).suggested] == [task.id]

    await pool.execute("UPDATE tasks SET expires_at = now() - interval '1 minute'")
    view = await tasks.today_view()
    assert view.suggested == []
    expired = await tasks.get(task.id)
    assert expired is not None and expired.status == "expired"
    with pytest.raises(TaskError, match="expired"):
        await tasks.confirm(task.id)


async def test_confirm_clears_expiry(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    task = await tasks.add(title="Renew the domain", source="ai_suggested", evidence=EVIDENCE)
    confirmed = await tasks.confirm(task.id)
    assert confirmed.status == "open"
    assert confirmed.expires_at is None
    await pool.execute("UPDATE tasks SET created_at = now() - interval '10 days'")
    assert await tasks.expire_suggestions() == 0


# -- defer_count ---------------------------------------------------------------


async def test_first_scheduling_is_not_a_deferral(tasks: TaskStore):
    task = await tasks.add(title="Plan the offsite")
    task = await tasks.defer(task.id, to="tomorrow")
    assert task.defer_count == 0
    assert task.planned_date == await tasks.today() + timedelta(days=1)


async def test_pushing_later_counts_and_pulling_earlier_does_not(tasks: TaskStore):
    today = await tasks.today()
    task = await tasks.add(title="Plan the offsite", planned_date=today)
    task = await tasks.defer(task.id, to="tomorrow")
    assert task.defer_count == 1
    task = await tasks.defer(task.id, to=today + timedelta(days=5))
    assert task.defer_count == 2
    task = await tasks.defer(task.id, to=today + timedelta(days=3))
    assert task.defer_count == 2
    task = await tasks.defer(task.id, to="someday")
    assert task.defer_count == 3
    assert task.someday and task.planned_date is None
    # Someday back to a date is planning it again, not deferring.
    task = await tasks.defer(task.id, to="tomorrow")
    assert task.defer_count == 3
    assert not task.someday


async def test_update_moving_planned_date_later_counts(tasks: TaskStore):
    today = await tasks.today()
    task = await tasks.add(title="Plan the offsite", planned_date=today)
    task = await tasks.update(task.id, planned_date=today + timedelta(days=2))
    assert task.defer_count == 1
    task = await tasks.update(task.id, title="Plan the team offsite")
    assert task.defer_count == 1


async def test_moving_a_task_due_today_counts(tasks: TaskStore):
    today = await tasks.today()
    task = await tasks.add(title="File the expense report", due_date=today)
    task = await tasks.defer(task.id, to="tomorrow")
    assert task.defer_count == 1


async def test_cannot_defer_into_the_past(tasks: TaskStore):
    task = await tasks.add(title="Plan the offsite")
    with pytest.raises(TaskError, match="past"):
        await tasks.defer(task.id, to=await tasks.today() - timedelta(days=1))


# -- views ---------------------------------------------------------------------


async def test_today_view_partitions_open_tasks(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    today = await tasks.today()
    late = await tasks.add(title="Late plan", planned_date=today - timedelta(days=1))
    missed = await tasks.add(title="Missed deadline", due_date=today - timedelta(days=2))
    replanned = await tasks.add(
        title="Missed but replanned",
        due_date=today - timedelta(days=2),
        planned_date=today + timedelta(days=1),
    )
    now = await tasks.add(title="Today plan", planned_date=today)
    due = await tasks.add(title="Due today", due_date=today)
    loose = await tasks.add(title="Loose end", due_date=today + timedelta(days=9))
    await tasks.add(title="Parked", someday=True)
    suggestion = await tasks.add(title="A suggestion", source="ai_suggested", evidence=EVIDENCE)
    finished = await tasks.add(title="Finished", planned_date=today)
    await tasks.done(finished.id)

    view = await tasks.today_view()
    assert {t.id for t in view.overdue} == {late.id, missed.id}
    assert {t.id for t in view.due_today} == {now.id, due.id}
    assert [t.id for t in view.unscheduled] == [loose.id]
    assert [t.id for t in view.suggested] == [suggestion.id]
    # Replanned for tomorrow, plus parked: arranged, nothing to think about.
    assert view.arranged == 2
    assert replanned.id not in {t.id for t in view.overdue}


async def test_week_view_and_project_stats(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    today = await tasks.today()
    a = await tasks.add(title="Week task", project="axiom", planned_date=today)
    await tasks.add(title="Other open", project="axiom", due_date=today + timedelta(days=3))
    await tasks.done(a.id)

    week = await tasks.week_view(today)
    assert [t.id for t in week.days[today.weekday()]] == [a.id]
    assert [t.id for t in week.done] == [a.id]

    stats = await tasks.project_stats()
    assert stats["axiom"].open == 1
    assert stats["axiom"].done_this_week == 1
    assert stats["axiom"].done_total == 1
    assert stats["axiom"].next_due == today + timedelta(days=3)

    await pool.execute("UPDATE tasks SET updated_at = now() - interval '15 days'")
    stats = await tasks.project_stats()
    assert [t.title for t in stats["axiom"].stale_open] == ["Other open"]


async def test_find_filters(tasks: TaskStore, pool: asyncpg.Pool):
    await _overview(pool)
    today = await tasks.today()
    await tasks.add(title="In project", project="axiom", planned_date=today)
    await tasks.add(title="Elsewhere", planned_date=today + timedelta(days=10))
    assert [t.title for t in await tasks.find(project="axiom")] == ["In project"]
    ranged = await tasks.find(date_from=today + timedelta(days=5))
    assert [t.title for t in ranged] == ["Elsewhere"]
    assert await tasks.find(statuses=["done"]) == []


def test_later_this_week_falls_back_to_next_week():
    from datetime import date

    wednesday = date(2026, 10, 21)
    assert later_this_week(wednesday) == [date(2026, 10, d) for d in (23, 24, 25)]
    saturday = date(2026, 10, 24)
    days = later_this_week(saturday)
    assert days[0] == date(2026, 10, 26) and days[-1] == date(2026, 11, 1)
