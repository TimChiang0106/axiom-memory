"""Memory persistence and retrieval on Postgres.

Write discipline lives here, not in client prompts: exact-name writes update in
place, and new writes are screened against existing memories (trigram
similarity) before insertion so near-duplicates are surfaced instead of
silently accumulating.
"""

import asyncio
from typing import Any

import asyncpg

from axiom.embeddings import GeminiEmbedder, to_pgvector
from axiom.models import (
    AppActivity,
    Category,
    DriftedOverview,
    DuplicatePair,
    Event,
    EventCluster,
    Memory,
    MemorySummary,
    MemoryType,
    MemoryVersion,
    Provenance,
    RememberResult,
    ReviewReport,
    StaleMemory,
)

# Project overviews follow this name suffix by convention (see the server
# instructions); staleness and orphan checks key off it.
_OVERVIEW_SUFFIX = "-overview"

# Memories left behind by a forgotten overview: they share its name prefix or
# were linked from its final version, and no live overview links them now.
# Retiring a context (a finished project, a job you left) means forgetting its
# overview; without this, its details would quietly keep surfacing in recall as
# if still in force. $2 narrows to one overview, or NULL for all of them.
_HEADLESS_SQL = """
    WITH gone AS (
        SELECT DISTINCT ON (v.name)
               v.name,
               left(v.name, length(v.name) - length($1)) AS prefix,
               v.related
        FROM memory_versions v
        WHERE v.reason = 'forgotten'
          AND v.name LIKE '%' || $1
          AND ($2::text IS NULL OR v.name = $2)
          AND NOT EXISTS (SELECT 1 FROM memories m WHERE m.name = v.name)
        ORDER BY v.name, v.superseded_at DESC, v.id DESC
    )
    SELECT DISTINCT m.name, m.title, m.description, m.type, m.category,
                    m.created_at, m.updated_at, m.archived
    FROM memories m
    JOIN gone g ON m.name LIKE g.prefix || '-%' OR m.name = ANY(g.related)
    WHERE NOT EXISTS (
        SELECT 1 FROM memories o
        WHERE o.name LIKE '%' || $1 AND m.name = ANY(o.related)
    )
    ORDER BY m.updated_at DESC
"""

# Text the search indexes are built over; must match the migration definitions.
_SEARCH_TEXT = "m.name || ' ' || m.description || ' ' || m.content"

_COLUMNS = (
    "m.name, m.description, m.content, m.type, m.source_app, "
    "m.created_at, m.updated_at, m.use_count, m.last_used_at, m.related, m.category, "
    "m.provenance, m.title, m.archived"
)


def _similar_pairs(
    items: list[tuple[Any, list[str]]], threshold: float
) -> dict[tuple[Any, Any], float]:
    """Pairs whose trigram similarity reaches `threshold`, scored the way
    pg_trgm's similarity() does (shared distinct trigrams over their union).
    Asking Postgres for similarity() pair by pair re-extracts both texts'
    trigrams every time — minutes at a few hundred rows — so each item's
    trigrams (from show_trgm) are compared here as bitmasks instead."""
    ids: dict[str, int] = {}
    masks = []
    for key, grams in items:
        mask = 0
        for gram in grams:
            mask |= 1 << ids.setdefault(gram, len(ids))
        masks.append((key, mask, len(grams)))
    found = {}
    for i, (key_a, a, len_a) in enumerate(masks):
        for key_b, b, len_b in masks[i + 1 :]:
            # Similarity can't exceed the size ratio; skip the AND.
            if not len_a or not len_b or min(len_a, len_b) < threshold * max(len_a, len_b):
                continue
            shared = (a & b).bit_count()
            score = shared / (len_a + len_b - shared)
            if score >= threshold:
                found[(key_a, key_b)] = score
    return found


class MemoryStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        dedup_threshold: float,
        recall_threshold: float,
        recall_half_life_days: float = 180.0,
        state_half_life_days: float = 30.0,
        procedural_half_life_days: float = 540.0,
        use_count_dampening: float = 0.0,
        lexical_weight: float = 0.4,
        inferred_weight: float = 0.8,
        stale_state_days: int = 90,
        event_similarity: float = 0.3,
        event_retention_days: int = 180,
        embedder: GeminiEmbedder | None = None,
        semantic_floor: float = 0.55,
        semantic_dedup_threshold: float | None = None,
        write_neighbor_threshold: float = 0.15,
        write_neighbor_count: int = 3,
    ):
        self._pool = pool
        self._dedup_threshold = dedup_threshold
        self._write_neighbor_threshold = write_neighbor_threshold
        self._write_neighbor_count = write_neighbor_count
        # Cosine similarity at which two memories count as saying the same
        # thing; None disables the embedding screen (see config for why).
        self._semantic_dedup_threshold = semantic_dedup_threshold
        self._recall_threshold = recall_threshold
        self._recall_half_life_days = recall_half_life_days
        self._state_half_life_days = state_half_life_days
        self._procedural_half_life_days = procedural_half_life_days
        self._use_count_dampening = use_count_dampening
        self._lexical_weight = lexical_weight
        self._inferred_weight = inferred_weight
        self._stale_state_days = stale_state_days
        self._event_similarity = event_similarity
        self._event_retention_days = event_retention_days
        self._embedder = embedder
        # Cosine similarity below this counts as zero relevance; above it,
        # rescaled to 0..1 so it shares a scale with the lexical scores.
        # Calibrated on real data: related pairs land ~0.65, unrelated ~0.50.
        self._semantic_floor = semantic_floor
        # The duplicate scan's last result, keyed by (row count, latest
        # update): any write or forget changes the key.
        self._dup_cache: tuple[tuple[int, Any], list[DuplicatePair]] | None = None

    async def remember(
        self,
        *,
        name: str,
        description: str,
        content: str,
        type: MemoryType,
        title: str = "",
        archived: bool | None = None,
        category: Category | None = None,
        source_app: str,
        related: list[str] | None = None,
        provenance: Provenance | None = None,
        allow_duplicate: bool = False,
    ) -> RememberResult:
        """Create or update one memory. provenance=None keeps an existing
        memory's provenance on update (so a routine edit never silently
        upgrades an inference to a stated fact) and means 'stated' on
        create. An empty title and archived=None likewise keep what is there."""
        related = related or []
        if category is None:
            if type == "preference":
                category = "you"
            elif type == "project":
                category = "areas"
            else:
                category = "topics"
        embedding = await self._embed_memory(name, description, content)
        async with self._pool.acquire() as conn:
            updated = await conn.fetchrow(
                f"""
                UPDATE memories m
                SET description = $2, content = $3, type = $4, category = $5, source_app = $6,
                    related = $7, embedding = $8::vector,
                    provenance = COALESCE($9, m.provenance),
                    title = COALESCE(NULLIF($10, ''), m.title),
                    archived = COALESCE($11, m.archived), updated_at = now()
                WHERE m.name = $1
                RETURNING {_COLUMNS}
                """,
                name,
                description,
                content,
                type,
                category,
                source_app,
                related,
                embedding,
                provenance,
                title,
                archived,
            )
            if updated is not None:
                return RememberResult(status="updated", memory=Memory.from_row(updated))

            search_text = f"{name} {description} {content}"
            if not allow_duplicate:
                similar = await self._find_similar(
                    conn,
                    search_text,
                    embedding if self._semantic_dedup_threshold is not None else None,
                )
                if similar:
                    return RememberResult(status="duplicate_suspected", similar=similar)

            # Advisory siblings — computed before the insert so the new name is
            # naturally excluded. The embedding is used even when the dedup
            # screen is off: project siblings score high semantically, which is
            # exactly what we want to surface here.
            neighbors = await self._neighbors(conn, search_text, embedding, exclude=name)

            created = await conn.fetchrow(
                f"""
                INSERT INTO memories AS m
                    (name, description, content, type, category, source_app, related,
                     embedding, provenance, title, archived)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::vector, $9, $10, $11)
                RETURNING {_COLUMNS}
                """,
                name,
                description,
                content,
                type,
                category,
                source_app,
                related,
                embedding,
                provenance or "stated",
                title,
                archived or False,
            )
            assert created is not None
            return RememberResult(
                status="created", memory=Memory.from_row(created), neighbors=neighbors
            )

    async def _embed_memory(self, name: str, description: str, content: str) -> str | None:
        """Embed the same text the lexical indexes cover. None (no embedder
        configured, or API failure) leaves the column NULL — the memory still
        matches lexically, and `axiom embed` backfills it later."""
        if self._embedder is None:
            return None
        values = await self._embedder.embed(f"{name} {description} {content}", kind="document")
        return to_pgvector(values) if values is not None else None

    async def recall(
        self,
        query: str,
        *,
        category: Category | None = None,
        limit: int = 5,
        track_usage: bool = True,
    ) -> list[Memory]:
        """Match on the strongest of full-text match, trigram word similarity
        (covers CJK and fuzzy matches), and — when an embedder is configured —
        semantic cosine similarity. Rank on a weighted blend of the lexical
        and semantic signals rather than their maximum: trigram similarity
        saturates at 1.0 for any long memory that mentions the query word
        once, and taking the max then throws away the semantic signal that
        tells such memories apart. The blend is decayed by recency: a memory
        unused for a long time sinks in the ranking (but is never filtered
        out). `state` memories decay faster; `procedural` ones decay slower —
        skills stay valid even when unused. An optional log-dampened
        use_count factor (ACT-R strengthening) is off by default: use_count
        counts every time a memory was surfaced, so boosting on it feeds back
        into itself and lets popular memories bury better matches. Decay and
        strengthening only reorder, as does the inferred_weight discount on
        memories an AI inferred rather than the user stated. `state` memories
        past the stale threshold
        come back flagged possibly_stale so callers verify before relying on
        them. An embedding-API failure silently degrades to lexical-only
        ranking; recall never breaks. track_usage=False skips the usage
        bump, so offline evaluation leaves the usage signal untouched."""
        query_vec: str | None = None
        if self._embedder is not None:
            values = await self._embedder.embed(query, kind="query")
            if values is not None:
                query_vec = to_pgvector(values)

        params: list = [
            query,
            self._recall_threshold,
            limit,
            self._state_half_life_days,
            self._recall_half_life_days,
            self._procedural_half_life_days,
            self._use_count_dampening,
            self._stale_state_days,
            self._lexical_weight,
            self._inferred_weight,
        ]
        # Cosine similarity below the floor scores zero; above it, rescaled to
        # 0..1 so it shares a scale with the lexical scores (ts_rank / trigram).
        # NULL means "no semantic signal" — the memory, or the query, has no
        # vector — and the blend falls back to lexical alone.
        if query_vec is not None:
            params += [query_vec, self._semantic_floor]
            semantic_expr = """
                CASE WHEN m.embedding IS NULL THEN NULL
                     ELSE GREATEST(
                         0::float8,
                         (1 - (m.embedding <=> $11::vector))::float8 - $12::float8
                     ) / (1::float8 - $12::float8)
                END"""
        else:
            semantic_expr = "NULL::float8"

        if category is not None:
            params.append(category)
            category_filter = f"AND sub.category = ${len(params)}"
        else:
            category_filter = ""

        rows = await self._pool.fetch(
            f"""
            SELECT {_COLUMNS.replace("m.", "sub.")},
                   (CASE WHEN sub.semantic IS NULL THEN sub.lexical
                         ELSE $9::float8 * sub.lexical + (1 - $9::float8) * sub.semantic
                    END
                    * sub.recency
                    * CASE WHEN $7::float8 > 0
                           THEN 1 + ln(1 + sub.use_count) / $7::float8
                           ELSE 1 END
                    * CASE WHEN sub.provenance = 'inferred' THEN $10::float8 ELSE 1 END
                   )::float8 AS score,
                   ((sub.type = 'state'
                     AND sub.updated_at < now() - make_interval(days => $8::int))
                    OR EXISTS (
                        SELECT 1 FROM memories r
                        WHERE r.name = ANY(sub.related) AND r.updated_at > sub.updated_at
                    )
                   ) AS possibly_stale
            FROM (
                SELECT {_COLUMNS},
                       m.search @@ websearch_to_tsquery('simple', $1) AS fts_hit,
                       GREATEST(
                           ts_rank(m.search, websearch_to_tsquery('simple', $1)),
                           word_similarity($1, {_SEARCH_TEXT})
                       )::float8 AS lexical,
                       ({semantic_expr})::float8 AS semantic,
                       exp(
                           -ln(2)
                           * (extract(epoch FROM now()
                                      - COALESCE(m.last_used_at, m.created_at)) / 86400.0)
                           / (CASE m.type WHEN 'state' THEN $4::float8
                                          WHEN 'procedural' THEN $6::float8
                                          ELSE $5::float8 END)
                       ) AS recency
                FROM memories m
            ) sub
            WHERE (sub.fts_hit OR GREATEST(sub.lexical, COALESCE(sub.semantic, 0)) >= $2)
                  {category_filter}
            ORDER BY score DESC, sub.updated_at DESC
            LIMIT $3
            """,
            *params,
        )
        memories = [Memory.from_row(r) for r in rows]
        # Retrieval strengthens memory: bump usage for everything we surfaced.
        # The returned objects keep their pre-bump counts, which reflects the
        # state at recall time.
        if memories and track_usage:
            await self._pool.execute(
                "UPDATE memories SET use_count = use_count + 1, last_used_at = now() "
                "WHERE name = ANY($1::text[])",
                [m.name for m in memories],
            )
        return memories

    async def get(self, name: str) -> Memory | None:
        """Fetch one memory by exact name. possibly_stale is computed the same
        way recall does it, so reading a project overview directly still flags
        that one of its details has moved on past it."""
        row = await self._pool.fetchrow(
            f"""
            SELECT {_COLUMNS},
                   ((m.type = 'state'
                     AND m.updated_at < now() - make_interval(days => $2::int))
                    OR EXISTS (
                        SELECT 1 FROM memories r
                        WHERE r.name = ANY(m.related) AND r.updated_at > m.updated_at
                    )
                   ) AS possibly_stale
            FROM memories m WHERE m.name = $1
            """,
            name,
            self._stale_state_days,
        )
        return Memory.from_row(row) if row else None

    async def forget(self, name: str) -> bool:
        """Delete one memory and scrub its name from every other memory's
        `related` list, so recall never points onward to a deleted entry."""
        async with self._pool.acquire() as conn, conn.transaction():
            result = await conn.execute("DELETE FROM memories WHERE name = $1", name)
            if result != "DELETE 1":
                return False
            await conn.execute(
                "UPDATE memories SET related = array_remove(related, $1) WHERE $1 = ANY(related)",
                name,
            )
            return True

    async def left_behind(self, overview: str) -> list[MemorySummary]:
        """Memories a forgotten overview left behind (see `_HEADLESS_SQL`) —
        what still has to be retired, or re-homed, along with it."""
        rows = await self._pool.fetch(_HEADLESS_SQL, _OVERVIEW_SUFFIX, overview)
        return [MemorySummary(**dict(r)) for r in rows]

    async def list_all(self, *, category: Category | None = None) -> list[MemorySummary]:
        if category is not None:
            rows = await self._pool.fetch(
                "SELECT name, title, description, type, category, created_at, updated_at, archived "
                "FROM memories "
                "WHERE category = $1 ORDER BY updated_at DESC",
                category,
            )
        else:
            rows = await self._pool.fetch(
                "SELECT name, title, description, type, category, created_at, updated_at, archived "
                "FROM memories "
                "ORDER BY updated_at DESC"
            )
        return [MemorySummary(**dict(r)) for r in rows]

    async def backlinks(self, name: str) -> list[MemorySummary]:
        """Memories whose `related` points at `name`."""
        rows = await self._pool.fetch(
            "SELECT name, title, description, type, category, created_at, updated_at, archived "
            "FROM memories "
            "WHERE $1 = ANY(related) ORDER BY updated_at DESC",
            name,
        )
        return [MemorySummary(**dict(r)) for r in rows]

    async def recent_events(self, *, limit: int = 20) -> list[Event]:
        rows = await self._pool.fetch(
            "SELECT id, content, source_app, created_at FROM events "
            "ORDER BY created_at DESC LIMIT $1",
            limit,
        )
        return [Event(**dict(r)) for r in rows]

    async def app_activity(self) -> list[AppActivity]:
        """Per client app, most recently active first."""
        rows = await self._pool.fetch(
            """
            SELECT source_app, sum(m)::int AS memories, sum(e)::int AS events,
                   max(last_at) AS last_at
            FROM (
                SELECT source_app, count(*) AS m, 0 AS e, max(updated_at) AS last_at
                FROM memories GROUP BY source_app
                UNION ALL
                SELECT source_app, 0, count(*), max(created_at) FROM events GROUP BY source_app
            ) t
            GROUP BY source_app
            ORDER BY last_at DESC
            """
        )
        return [AppActivity(**dict(r)) for r in rows]

    async def dismissed(self) -> set[tuple[str, str]]:
        """(kind, key) of review items the user chose to keep."""
        rows = await self._pool.fetch("SELECT kind, key FROM review_dismissals")
        return {(r["kind"], r["key"]) for r in rows}

    async def dismiss(self, kind: str, key: str) -> None:
        await self._pool.execute(
            "INSERT INTO review_dismissals (kind, key) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            kind,
            key,
        )

    async def history(self, name: str, *, limit: int = 20) -> list[MemoryVersion]:
        """Past versions of a memory, newest first — including the final state
        of a forgotten one. Recorded by a database trigger (migration 0008)."""
        rows = await self._pool.fetch(
            """
            SELECT name, description, content, type, category, provenance, source_app,
                   related, written_at, superseded_at, reason
            FROM memory_versions
            WHERE name = $1
            ORDER BY superseded_at DESC, id DESC
            LIMIT $2
            """,
            name,
            limit,
        )
        return [MemoryVersion(**{**dict(r), "related": list(r["related"])}) for r in rows]

    async def index_nearest(
        self, query: str, *, category: Category | None = None, limit: int = 25
    ) -> list[MemorySummary]:
        """The closest index entries to a query, with no relevance cutoff —
        the fallback shown when recall misses. Bounded, unlike the full index,
        so a miss costs the caller a fixed amount of context however large the
        store grows."""
        params: list = [query, limit]
        category_filter = ""
        if category is not None:
            params.append(category)
            category_filter = f"WHERE m.category = ${len(params)}"
        rows = await self._pool.fetch(
            f"""
            SELECT m.name, m.title, m.description, m.type, m.category,
                   m.created_at, m.updated_at, m.archived
            FROM memories m
            {category_filter}
            ORDER BY word_similarity($1, {_SEARCH_TEXT}) DESC, m.updated_at DESC
            LIMIT $2
            """,
            *params,
        )
        return [MemorySummary(**dict(r)) for r in rows]

    async def summaries_by_names(self, names: list[str]) -> list[MemorySummary]:
        """Index entries for the given names (missing names are skipped),
        in the order requested. Used to expand `related` links one hop."""
        if not names:
            return []
        rows = await self._pool.fetch(
            """
            SELECT name, title, description, type, category, created_at, updated_at, archived
            FROM memories
            WHERE name = ANY($1::text[])
            ORDER BY array_position($1::text[], name)
            """,
            names,
        )
        return [MemorySummary(**dict(r)) for r in rows]

    async def all_memories(self) -> list[Memory]:
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM memories m ORDER BY m.category, m.name"
        )
        return [Memory.from_row(r) for r in rows]

    async def backfill_embeddings(self) -> tuple[int, int]:
        """Embed every memory whose vector is missing (written before
        embeddings existed, or while the API was down). Returns
        (embedded, still_missing)."""
        if self._embedder is None:
            raise RuntimeError("No embedder configured — set AXIOM_GEMINI_API_KEY.")
        rows = await self._pool.fetch(
            "SELECT name, description, content FROM memories WHERE embedding IS NULL"
        )
        embedded = 0
        for row in rows:
            vec = await self._embed_memory(row["name"], row["description"], row["content"])
            if vec is None:
                continue
            await self._pool.execute(
                "UPDATE memories SET embedding = $2::vector WHERE name = $1",
                row["name"],
                vec,
            )
            embedded += 1
        return embedded, len(rows) - embedded

    async def log_event(self, *, content: str, source_app: str) -> Event:
        """Append one episodic event and prune the layer's tail: events older
        than the retention window are deleted outright — this layer is
        designed to forget, unlike memories which only sink in ranking."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO events (content, source_app)
                VALUES ($1, $2)
                RETURNING id, content, source_app, created_at
                """,
                content,
                source_app,
            )
            assert row is not None
            await conn.execute(
                "DELETE FROM events WHERE created_at < now() - ($1 * interval '1 day')",
                self._event_retention_days,
            )
            return Event(**dict(row))

    async def recurring_events(self, *, window_days: int, min_count: int) -> list[EventCluster]:
        """Cluster recent events by trigram similarity (single-link over
        similar pairs) and return clusters big enough to suggest a pattern.
        Volumes are personal-scale, so pairing and grouping in Python
        is plenty."""
        events = await self._pool.fetch(
            """
            SELECT id, content, created_at, show_trgm(content) AS g FROM events
            WHERE created_at >= now() - ($1 * interval '1 day')
            ORDER BY created_at DESC
            """,
            window_days,
        )
        if len(events) < min_count:
            return []
        pairs = await asyncio.to_thread(
            _similar_pairs, [(e["id"], e["g"]) for e in events], self._event_similarity
        )

        parent: dict[int, int] = {r["id"]: r["id"] for r in events}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for id_a, id_b in pairs:
            parent[find(id_a)] = find(id_b)

        by_root: dict[int, list[asyncpg.Record]] = {}
        for e in events:  # already newest-first
            by_root.setdefault(find(e["id"]), []).append(e)

        clusters = [
            EventCluster(
                count=len(group),
                first_at=group[-1]["created_at"],
                last_at=group[0]["created_at"],
                samples=[e["content"] for e in group[:3]],
            )
            for group in by_root.values()
            if len(group) >= min_count
        ]
        clusters.sort(key=lambda c: c.count, reverse=True)
        return clusters

    async def consolidation_candidates(
        self,
        *,
        stale_state_days: int,
        zombie_days: int,
        zombie_max_use_count: int = 2,
        event_window_days: int = 30,
        event_min_count: int = 3,
    ) -> ReviewReport:
        """Surface memories worth a human's attention — the minimal, offline
        version of sleep-time consolidation. It only reports; it never edits or
        deletes. Six buckets: near-duplicate pairs (merge?), `state` memories
        left untouched too long (still true?), memories that have barely ever
        surfaced in recall (worth keeping?), project overviews that have fallen
        behind their own details (refresh the source of truth?), project notes
        orphaned from their overview (fold in?), and recurring episodic events
        (promote to a memory?)."""
        dup_pairs = await self._duplicate_pairs()
        stale_rows = await self._pool.fetch(
            """
            SELECT name, type, category, COALESCE(last_used_at, updated_at) AS since
            FROM memories
            WHERE type = 'state'
              AND COALESCE(last_used_at, updated_at) < now() - ($1 * interval '1 day')
            ORDER BY since
            """,
            stale_state_days,
        )
        # use_count counts every time recall *surfaced* a memory, not that a
        # client actually used it, so it runs high: anything at or below the
        # small allowance that also hasn't surfaced recently is effectively
        # dead, not just the strictly-never-recalled.
        zombie_rows = await self._pool.fetch(
            """
            SELECT name, type, category, COALESCE(last_used_at, created_at) AS since
            FROM memories
            WHERE use_count <= $2
              AND created_at < now() - ($1 * interval '1 day')
              AND COALESCE(last_used_at, created_at) < now() - ($1 * interval '1 day')
            ORDER BY since
            """,
            zombie_days,
            zombie_max_use_count,
        )
        headless_rows = await self._pool.fetch(_HEADLESS_SQL, _OVERVIEW_SUFFIX, None)
        return ReviewReport(
            duplicates=dup_pairs,
            stale_state=[StaleMemory(**dict(r)) for r in stale_rows],
            zombies=[StaleMemory(**dict(r)) for r in zombie_rows],
            drifted_overviews=await self.drifted_overviews(),
            orphans=await self.orphans(),
            headless=[MemorySummary(**dict(r)) for r in headless_rows],
            recurring=await self.recurring_events(
                window_days=event_window_days, min_count=event_min_count
            ),
        )

    async def _duplicate_pairs(self) -> list[DuplicatePair]:
        """Near-duplicate pairs. Lexical pairs catch same-wording duplicates;
        embedding pairs, when the semantic screen is enabled, catch
        paraphrases and cross-language restatements the trigrams miss.

        The lexical side is `_similar_pairs` over each memory's trigrams.
        Cached until a memory is written or forgotten."""
        key = tuple(await self._pool.fetchrow("SELECT count(*), max(updated_at) FROM memories"))
        if self._dup_cache and self._dup_cache[0] == key:
            return self._dup_cache[1]
        rows = await self._pool.fetch(
            f"SELECT m.name, show_trgm({_SEARCH_TEXT}) AS g FROM memories m ORDER BY m.name"
        )
        pairs = await asyncio.to_thread(
            _similar_pairs, [(r["name"], r["g"]) for r in rows], self._dedup_threshold
        )
        if self._semantic_dedup_threshold is not None:
            for r in await self._pool.fetch(
                """
                SELECT a.name AS name_a, b.name AS name_b,
                       (1 - (a.embedding <=> b.embedding))::float8 AS similarity
                FROM memories a JOIN memories b ON a.name < b.name
                WHERE a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                  AND 1 - (a.embedding <=> b.embedding) >= $1
                """,
                self._semantic_dedup_threshold,
            ):
                key = (r["name_a"], r["name_b"])
                pairs[key] = max(pairs.get(key, 0.0), r["similarity"])
        found = [
            DuplicatePair(name_a=a, name_b=b, similarity=score)
            for (a, b), score in sorted(pairs.items(), key=lambda kv: -kv[1])
        ]
        self._dup_cache = (key, found)
        return found

    async def drifted_overviews(self) -> list[DriftedOverview]:
        """Project overviews their own linked details have overtaken: the
        single source of truth is behind what it points at (the
        gateway-rollout case — a verification result newer than the plan it
        should revise)."""
        rows = await self._pool.fetch(
            """
            SELECT o.name, o.updated_at,
                   array_agg(r.name ORDER BY r.updated_at DESC) AS newer_related
            FROM memories o
            JOIN memories r ON r.name = ANY(o.related) AND r.updated_at > o.updated_at
            WHERE o.name LIKE '%' || $1
            GROUP BY o.name, o.updated_at
            ORDER BY o.updated_at
            """,
            _OVERVIEW_SUFFIX,
        )
        return [
            DriftedOverview(
                name=r["name"], updated_at=r["updated_at"], newer_related=list(r["newer_related"])
            )
            for r in rows
        ]

    async def orphans(self) -> list[MemorySummary]:
        """Notes that belong to a project with an overview — they share its
        name prefix — but aren't linked from it: the stray fifth memory the
        overview should have folded in. Scoped to prefixes that actually have
        an overview, so projects with no overview don't flood the bucket."""
        rows = await self._pool.fetch(
            """
            WITH overviews AS (
                SELECT name,
                       left(name, length(name) - length($1)) AS prefix,
                       related
                FROM memories
                WHERE name LIKE '%' || $1
            )
            SELECT DISTINCT m.name, m.title, m.description, m.type, m.category,
                            m.created_at, m.updated_at, m.archived
            FROM memories m
            JOIN overviews o ON m.name LIKE o.prefix || '-%'
            WHERE m.name NOT LIKE '%' || $1
              AND NOT (m.name = ANY(o.related))
            ORDER BY m.updated_at DESC
            """,
            _OVERVIEW_SUFFIX,
        )
        return [MemorySummary(**dict(r)) for r in rows]

    async def overviews(self) -> list[Memory]:
        """Every `<project>-overview` memory, for the project page."""
        rows = await self._pool.fetch(
            f"SELECT {_COLUMNS} FROM memories m WHERE m.name LIKE '%' || $1 ORDER BY m.name",
            _OVERVIEW_SUFFIX,
        )
        return [Memory.from_row(r) for r in rows]

    async def _neighbors(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        text: str,
        embedding: str | None,
        *,
        exclude: str,
    ) -> list[MemorySummary]:
        """The existing memories most similar to one being written — advisory
        context, not a gate. Unlike the dedup screen (`_find_similar`) this uses
        a low bar and never blocks; it surfaces the few nearest so the writer
        can rewrite a drifting overview or plan instead of adding a parallel.
        Returns summaries, not bodies, to stay cheap on the caller's context —
        the writer can `get` the one that looks relevant."""
        # Symmetric trigram similarity (doc-vs-doc), same signal and scale as
        # the dedup screen, just a lower bar — not word_similarity, which is for
        # short-query-vs-doc and reads low when the first arg is a long memory.
        sim = f"""
            GREATEST(
                similarity({_SEARCH_TEXT}, $1),
                CASE WHEN $2::text IS NULL OR m.embedding IS NULL THEN 0
                     ELSE 1 - (m.embedding <=> ($2::text)::vector)
                END
            )"""
        rows = await conn.fetch(
            f"""
            SELECT m.name, m.title, m.description, m.type, m.category,
                   m.created_at, m.updated_at, m.archived
            FROM memories m
            WHERE m.name <> $3 AND ({sim})::float8 >= $4
            ORDER BY ({sim})::float8 DESC, m.updated_at DESC
            LIMIT $5
            """,
            text,
            embedding,
            exclude,
            self._write_neighbor_threshold,
            self._write_neighbor_count,
        )
        return [
            MemorySummary(
                name=r["name"],
                title=r["title"],
                description=r["description"],
                type=r["type"],
                category=r["category"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                archived=r["archived"],
            )
            for r in rows
        ]

    async def _find_similar(
        self,
        conn: asyncpg.Connection | asyncpg.pool.PoolConnectionProxy,
        text: str,
        embedding: str | None,
    ) -> list[Memory]:
        """Trigram screening catches lexical near-duplicates; the optional
        embedding screen (embedding given) catches paraphrases and
        cross-language restatements (a Chinese memory duplicating an English
        one shares no trigrams at all). Either signal alone is enough to
        flag."""
        rows = await conn.fetch(
            f"""
            SELECT {_COLUMNS},
                   GREATEST(
                       similarity({_SEARCH_TEXT}, $1),
                       CASE WHEN $3::text IS NULL OR m.embedding IS NULL THEN 0
                            ELSE 1 - (m.embedding <=> ($3::text)::vector)
                       END
                   )::float8 AS score
            FROM memories m
            WHERE similarity({_SEARCH_TEXT}, $1) >= $2
               OR ($3::text IS NOT NULL AND m.embedding IS NOT NULL
                   AND 1 - (m.embedding <=> ($3::text)::vector) >= $4)
            ORDER BY score DESC
            LIMIT 3
            """,
            text,
            self._dedup_threshold,
            embedding,
            self._semantic_dedup_threshold,
        )
        return [Memory.from_row(r) for r in rows]
