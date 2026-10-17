"""Semantic recall: vector similarity surfaces memories with zero lexical
overlap, and every embedding failure degrades to lexical-only — never breaks."""

import asyncpg
import pytest

from axiom.store import MemoryStore

DIMS = 768


def unit(*components: tuple[int, float]) -> list[float]:
    vec = [0.0] * DIMS
    for index, value in components:
        vec[index] = value
    return vec


class FakeEmbedder:
    """Maps substring keys to fixed vectors; unknown text embeds to None,
    which is exactly what a failed API call looks like to the store."""

    dims = DIMS

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    async def embed(self, text: str, *, kind: str) -> list[float] | None:
        for key, vec in self._vectors.items():
            if key in text:
                return vec
        return None


@pytest.fixture
async def semantic_store(pool: asyncpg.Pool) -> MemoryStore:
    await pool.execute("TRUNCATE memories, events, memory_versions")
    embedder = FakeEmbedder(
        {
            # cos(query, interview-doc) = 0.8 — above the 0.55 floor.
            "會議": unit((0, 1.0)),
            "需求訪談": unit((0, 0.8), (1, 0.6)),
            # Orthogonal to the query — semantic score 0.
            "investment": unit((2, 1.0)),
        }
    )
    return MemoryStore(
        pool,
        dedup_threshold=0.35,
        recall_threshold=0.1,
        embedder=embedder,  # type: ignore[arg-type] — duck-typed test double
        semantic_floor=0.55,
    )


async def test_semantic_recall_hits_without_lexical_overlap(semantic_store: MemoryStore):
    await semantic_store.remember(
        name="client-discovery",
        description="製造業客戶 AI Agent 導入案的需求訪談重點",
        content="需求訪談:EIP 是否唯一入口、SSO 傳遞、第一期範圍。",
        type="project",
        source_app="test",
    )
    await semantic_store.remember(
        name="investment-filters",
        description="investment idea criteria",
        content="Prioritize lower-priced public companies.",
        type="preference",
        source_app="test",
        allow_duplicate=True,
    )

    # "meeting 會議" shares no words with the stored memory; only the vector
    # path can surface it.
    results = await semantic_store.recall("meeting 會議")
    assert [m.name for m in results] == ["client-discovery"]


async def test_recall_falls_back_to_lexical_when_query_embedding_fails(
    semantic_store: MemoryStore,
):
    await semantic_store.remember(
        name="investment-filters",
        description="investment idea criteria",
        content="Prioritize lower-priced public companies.",
        type="preference",
        source_app="test",
    )
    # "investment" matches lexically; the query embeds to None (unknown key
    # minus the known substrings), so only the lexical path runs.
    results = await semantic_store.recall("lower-priced investment")
    assert [m.name for m in results] == ["investment-filters"]


async def test_semantic_signal_breaks_saturated_lexical_ties(pool: asyncpg.Pool):
    # Both memories contain "Dana", so trigram word similarity is 1.0 for
    # each. Ranking on the max of the signals tied them and fell back to
    # recency, burying the real match under any long note that mentions the
    # name; the blend lets the semantic signal decide.
    await pool.execute("TRUNCATE memories, events, memory_versions")
    embedder = FakeEmbedder(
        {
            "contact-card": unit((0, 0.9), (1, 0.436)),
            "pipeline-log": unit((2, 1.0)),
            "Dana": unit((0, 1.0)),
        }
    )
    store = MemoryStore(
        pool,
        dedup_threshold=0.35,
        recall_threshold=0.1,
        embedder=embedder,  # type: ignore[arg-type] — duck-typed test double
    )
    await store.remember(
        name="dana-contact-card",
        description="Dana, sales lead",
        content="Dana leads financial-industry sales.",
        type="fact",
        source_app="test",
    )
    await store.remember(
        name="pipeline-log",
        description="Client pipeline status",
        content="Quote sent to Dana; gateway plan; DLP rounds; SOW drafts.",
        type="project",
        source_app="test",
        allow_duplicate=True,
    )
    results = await store.recall("Dana")
    assert results[0].name == "dana-contact-card"


async def test_dedup_catches_cross_language_paraphrase(pool: asyncpg.Pool):
    # An English memory and its Chinese restatement share no trigrams, so
    # only the embedding screen can connect them. Both keys map to nearly
    # the same direction: cos ≈ 0.98, above the 0.8 dedup threshold.
    await pool.execute("TRUNCATE memories, events, memory_versions")
    embedder = FakeEmbedder(
        {
            "oat milk": unit((0, 1.0)),
            "燕麥奶": unit((0, 0.98), (1, 0.199)),
        }
    )
    store = MemoryStore(
        pool,
        dedup_threshold=0.35,
        recall_threshold=0.1,
        embedder=embedder,  # type: ignore[arg-type] — duck-typed test double
        semantic_dedup_threshold=0.80,
    )
    await store.remember(
        name="coffee-preference",
        description="Coffee order",
        content="Always orders oat milk lattes.",
        type="preference",
        source_app="test",
    )
    result = await store.remember(
        name="latte-choice",
        description="咖啡偏好",
        content="拿鐵都點燕麥奶。",
        type="preference",
        source_app="test",
    )
    assert result.status == "duplicate_suspected"
    assert [m.name for m in result.similar] == ["coffee-preference"]

    # The review report's duplicate pairs use the same embedding screen.
    forced = await store.remember(
        name="latte-choice",
        description="咖啡偏好",
        content="拿鐵都點燕麥奶。",
        type="preference",
        source_app="test",
        allow_duplicate=True,
    )
    assert forced.status == "created"
    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)
    assert frozenset(("coffee-preference", "latte-choice")) in {
        frozenset((d.name_a, d.name_b)) for d in report.duplicates
    }


async def test_semantic_dedup_is_off_by_default(pool: asyncpg.Pool):
    # Sibling memories of one project embed close together (successive test
    # rounds score ~0.9 on real data), so the embedding screen must not
    # bounce writes unless explicitly enabled.
    await pool.execute("TRUNCATE memories, events, memory_versions")
    embedder = FakeEmbedder(
        {
            "round one": unit((0, 1.0)),
            "round two": unit((0, 0.98), (1, 0.199)),
        }
    )
    store = MemoryStore(
        pool,
        dedup_threshold=0.35,
        recall_threshold=0.1,
        embedder=embedder,  # type: ignore[arg-type] — duck-typed test double
    )
    await store.remember(
        name="dlp-acceptance-r1",
        description="DLP acceptance round one",
        content="Correct 492/660, leaked 109.",
        type="project",
        source_app="test",
    )
    result = await store.remember(
        name="dlp-acceptance-r2",
        description="驗收 round two",
        content="正確 625/660, 外洩 7。",
        type="project",
        source_app="test",
    )
    assert result.status == "created"
    report = await store.consolidation_candidates(stale_state_days=90, zombie_days=60)
    assert report.duplicates == []


async def test_remember_survives_embedding_failure(semantic_store: MemoryStore, pool):
    # No fake-embedder key matches → document embedding is None.
    result = await semantic_store.remember(
        name="unembeddable-memory",
        description="stored while the embedding API was down",
        content="Recall must still find this lexically.",
        type="fact",
        source_app="test",
    )
    assert result.status == "created"
    stored = await pool.fetchval(
        "SELECT embedding IS NULL FROM memories WHERE name = 'unembeddable-memory'"
    )
    assert stored is True

    results = await semantic_store.recall("embedding API down")
    assert [m.name for m in results] == ["unembeddable-memory"]
