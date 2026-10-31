"""Domain models shared by the store, the MCP tools, and the exporter."""

import json
from datetime import date, datetime
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field

MemoryType = Literal["preference", "fact", "project", "state", "reference", "procedural"]
Category = Literal["you", "people", "areas", "topics"]
# stated: the user said it. inferred: an AI concluded it from indirect
# evidence — ranks lower and should be confirmed before it is relied on.
Provenance = Literal["stated", "inferred"]

NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"


class Memory(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    description: str
    content: str
    type: MemoryType
    category: Category = "areas"
    provenance: Provenance = "stated"
    source_app: str
    created_at: datetime
    updated_at: datetime
    # Usage signal, bumped on every recall (see MemoryStore.recall). last_used_at
    # is None until the memory is recalled for the first time.
    use_count: int = 0
    last_used_at: datetime | None = None
    # Names of related memories, so recall results can point onward to
    # neighbours the caller may also want to pull.
    related: list[str] = []
    score: float | None = None
    # Computed at read time, never stored. True when either a `state` memory is
    # older than the stale threshold, or a memory whose `related` links point at
    # something updated more recently than itself — the classic case being a
    # project overview left behind while one of its detail memories moved on.
    # The caller should verify the content before relying on it, and
    # re-remember it to refresh.
    possibly_stale: bool = False

    def payload(self, max_chars: int | None = None) -> dict:
        """The memory as returned to MCP clients. With max_chars, long
        content is cut and flagged truncated, so a few long memories in one
        recall cannot flood the caller's context."""
        payload = self.model_dump(exclude={"score"})
        if max_chars is not None and len(self.content) > max_chars:
            payload["content"] = self.content[:max_chars] + "…"
            payload["truncated"] = True
        return payload

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> "Memory":
        return cls(
            name=row["name"],
            description=row["description"],
            content=row["content"],
            type=row["type"],
            category=row.get("category", "areas"),
            provenance=row.get("provenance", "stated"),
            source_app=row["source_app"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            use_count=row.get("use_count", 0),
            last_used_at=row.get("last_used_at"),
            related=list(row.get("related") or []),
            score=row.get("score"),
            possibly_stale=row.get("possibly_stale") or False,
        )


class MemorySummary(BaseModel):
    """Index entry: everything needed to decide relevance without the body."""

    name: str
    description: str
    type: MemoryType
    category: Category
    updated_at: datetime


class RememberResult(BaseModel):
    status: Literal["created", "updated", "duplicate_suspected"]
    memory: Memory | None = None
    # Populated when status is duplicate_suspected: the existing memories the
    # new one collided with. The caller should update one of these by name, or
    # retry with allow_duplicate=True if it is genuinely new.
    similar: list[Memory] = []
    # Populated on create: the existing memories most similar to the new one,
    # below the duplicate bar but close enough to be worth a look. Advisory, not
    # a block — surfaced so the writer notices a project's overview, plan, or
    # earlier decision and rewrites it instead of silently adding a parallel
    # memory that quietly contradicts it.
    neighbors: list[MemorySummary] = []


class MemoryVersion(BaseModel):
    """A past version of a memory: what it said before an update replaced it,
    or its final state before it was forgotten."""

    name: str
    description: str
    content: str
    type: MemoryType
    category: Category
    provenance: Provenance = "stated"
    source_app: str
    related: list[str] = []
    written_at: datetime
    superseded_at: datetime
    reason: Literal["updated", "forgotten"]


class DuplicatePair(BaseModel):
    """Two memories similar enough to be worth merging."""

    name_a: str
    name_b: str
    similarity: float


class DriftedOverview(BaseModel):
    """A project overview whose linked detail memories were updated more
    recently than the overview itself — the single source of truth may be
    behind. `newer_related` lists the details that moved on, newest first."""

    name: str
    updated_at: datetime
    newer_related: list[str]


class StaleMemory(BaseModel):
    """A memory flagged for review, with the timestamp that triggered it
    (last use / update for stale state, last surfacing — or creation, if
    never surfaced — for zombies)."""

    name: str
    type: MemoryType
    category: Category = "areas"
    since: datetime


class Event(BaseModel):
    """One episodic event: a weak ambient signal, not a curated fact.
    Events never enter recall; recurring patterns among them are surfaced by
    `axiom review` as candidates for promotion to a real memory."""

    id: int
    content: str
    source_app: str
    created_at: datetime


class AppActivity(BaseModel):
    """What one client app has written: memories it last wrote, events it
    logged, and when it last did either."""

    source_app: str
    memories: int
    events: int
    last_at: datetime


class EventCluster(BaseModel):
    """A group of similar recent events — evidence of a recurring pattern
    worth promoting to a memory (e.g. many cooking questions → a preference)."""

    count: int
    first_at: datetime
    last_at: datetime
    # Up to a few sample contents, most recent first, to show what the
    # pattern is about without dumping every event.
    samples: list[str]


class ReviewReport(BaseModel):
    """Consolidation candidates surfaced by `axiom review` — never acted on
    automatically; a human decides what to merge, refresh, or forget."""

    duplicates: list[DuplicatePair] = []
    stale_state: list[StaleMemory] = []
    zombies: list[StaleMemory] = []
    # Project overviews that have fallen behind their own detail memories.
    drifted_overviews: list[DriftedOverview] = []
    # Memories that belong to a project with an overview (they share its name
    # prefix) but are not linked from it — stray notes the overview should
    # fold in, the way a loose progress update slips past the source of truth.
    orphans: list[MemorySummary] = []
    # Memories whose overview was forgotten (they share its prefix or were
    # linked from it) and that no live overview links: the leftovers of a
    # retired project or job, still surfacing in recall — forget or re-home.
    headless: list[MemorySummary] = []
    # Recurring episodic patterns; promotion candidates, not yet memories.
    recurring: list[EventCluster] = []


TaskStatus = Literal["suggested", "open", "done", "dropped", "expired"]
# stated: the user said it (open at once). ai_suggested: an AI proposed it
# (lands as 'suggested' and waits for the user's confirm or reject).
TaskSource = Literal["stated", "ai_suggested"]
# The only reasons an AI may propose a task — each must cite memories.
SuggestionTrigger = Literal["unrecorded_commitment", "due_soon", "overview_stale"]


class TaskEvidence(BaseModel):
    """Why a task exists: what prompted it and which memories back it."""

    trigger: str | None = None
    memories: list[str] = []
    note: str | None = None


class TaskState(BaseModel):
    """The user's word for where an open task stands. `waiting` means someone
    else holds it, so it cannot be overdue."""

    name: str
    waiting: bool = False


class Task(BaseModel):
    id: int
    title: str
    project: str | None = None
    status: TaskStatus
    source: TaskSource
    due_date: date | None = None
    planned_date: date | None = None
    someday: bool = False
    defer_count: int = 0
    state: str | None = None
    waiting: bool = False
    waiting_on: str | None = None
    phase: str | None = None
    code: str | None = None
    evidence: TaskEvidence | None = None
    reject_reason: str | None = None
    done_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> "Task":
        data = {k: row[k] for k in cls.model_fields if k != "evidence"}
        raw = row["evidence"]
        data["evidence"] = TaskEvidence(**json.loads(raw)) if raw else None
        return cls(**data)


class DocumentSummary(BaseModel):
    name: str
    title: str
    project: str | None = None
    chars: int
    updated_at: datetime


class Document(BaseModel):
    name: str
    title: str
    body: str
    project: str | None = None
    source_app: str
    created_at: datetime
    updated_at: datetime


class DocumentVersion(BaseModel):
    """A past version of a document, as it read before an overwrite."""

    title: str
    body: str
    project: str | None = None
    source_app: str
    written_at: datetime
    superseded_at: datetime
