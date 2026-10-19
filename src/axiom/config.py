"""Runtime configuration, loaded from environment variables with the AXIOM_ prefix."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AXIOM_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://axiom:axiom@localhost:5432/axiom"

    # Google OAuth for interactive clients (ChatGPT and claude.ai connectors
    # require a full OAuth flow). All three must be set together; base_url is
    # the public origin OAuth endpoints are served from, e.g.
    # https://axiom.example.com (or http://localhost:8080 when testing the
    # flow locally — both must be registered as redirect URIs on the Google
    # OAuth app, with the /auth/callback path).
    google_client_id: str = ""
    google_client_secret: str = ""
    base_url: str = ""

    # Comma-separated emails allowed through Google login. This server holds
    # one person's memory: anyone else who authenticates is rejected.
    allowed_emails: str = ""

    # Static bearer token for headless callers (curl, CI, cron), checked
    # alongside OAuth. Empty disables it. Clients send
    # `Authorization: Bearer <token>`.
    service_token: str = ""

    # Cloudflare Access in front of the /app pages: the team domain (e.g.
    # https://yourteam.cloudflareaccess.com) and the Access application's
    # AUD tag. When both are set, /app accepts a valid Access JWT for an
    # AXIOM_ALLOWED_EMAILS address; the service token works either way.
    cf_access_team_domain: str = ""
    cf_access_aud: str = ""

    # The user's time zone (IANA name): decides what "today" means for tasks.
    timezone: str = "UTC"

    # Task rules for AI suggestions (see axiom.tasks).
    suggestion_daily_limit: int = 3  # per rolling 24 hours
    suggestion_ttl_days: int = 5  # unanswered suggestions expire after this
    rejected_window_days: int = 30  # rejected suggestions block look-alikes this long
    # A suggestion this similar to an open/suggested task or a recent rejection
    # is refused: trigram similarity on titles, or title-embedding cosine.
    task_similar_trigram: float = 0.5
    # Calibrated on Gemini title embeddings: paraphrases (incl. zh/en) 0.87-0.93,
    # different tasks of one project <= 0.81.
    task_similar_semantic: float = 0.85
    # Open tasks not updated for this long get asked "still doing this?".
    stale_open_days: int = 14

    # HTTP transport.
    host: str = "127.0.0.1"
    port: int = 8080

    # Markdown export target (a local checkout; commit/push is left to the caller).
    export_dir: Path = Path("exports")

    # Semantic recall (Gemini embeddings). Empty key disables the semantic
    # layer entirely — recall degrades to lexical-only, nothing breaks.
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-2"
    # Must match the vector(N) column in migration 0005.
    embedding_dims: int = 768
    # Cosine similarity below this counts as zero relevance; above it, it is
    # rescaled to 0..1. Calibrated on real memories: related pairs ~0.65,
    # unrelated ~0.50.
    semantic_floor: float = 0.55
    # Recall's query embedding gives up after this long and ranks lexically.
    embedding_query_timeout_seconds: float = 3.0

    # recall returns at most this many characters of each memory's content
    # (flagged truncated); the `get` tool returns the full text.
    recall_content_chars: int = 1500

    # Minimum trigram similarity for a new memory to be flagged as a
    # suspected duplicate of an existing one.
    dedup_threshold: float = 0.35
    # Optional embedding cosine threshold for the same flag, to catch
    # paraphrases and cross-language duplicates that share no trigrams. Off
    # (None) by default: on real data, sibling memories of one project (e.g.
    # successive test rounds) score 0.85-0.96 doc-to-doc, so no threshold
    # separates true duplicates from siblings — at 0.80, 1,335 of ~57k pairs
    # were flagged and nearly every project write would bounce.
    semantic_dedup_threshold: float | None = None

    # On every create, remember returns the few existing memories most similar
    # to the new one (below the duplicate bar) as advisory context, so the
    # writer can spot a project's overview/plan/earlier decision and rewrite it
    # rather than add a silent parallel. A low bar on purpose — it only
    # surfaces, never blocks — so false neighbours cost nothing.
    write_neighbor_threshold: float = 0.15
    write_neighbor_count: int = 3

    # Minimum combined score (FTS rank or trigram word similarity) for a
    # memory to appear in recall results.
    recall_threshold: float = 0.1

    # Retrieval-based forgetting: recall relevance is multiplied by a recency
    # factor that halves every N days since a memory was last used (or created,
    # if never recalled). This only reorders results — it never filters them
    # (the recall_threshold above still runs on raw relevance). `state` memories
    # are meant to go stale, so they decay faster than everything else.
    recall_half_life_days: float = 180.0
    state_half_life_days: float = 30.0
    # Procedural memories (how-to workflows, behavioural rules) are the most
    # decay-resistant kind — a workflow stays valid even when unused for
    # months — so they outlast everything else in the ranking.
    procedural_half_life_days: float = 540.0

    # Recall ranks on lexical_weight * lexical + (1 - lexical_weight) * semantic
    # (lexical alone when either side has no vector). Chosen by `axiom eval`
    # on 48 labelled cases: 0.3 to 0.4 all score ~96% hit@5; taking the max of
    # the two signals (the old ranking) scored 73%.
    lexical_weight: float = 0.4

    # Recall score multiplier for memories an AI inferred rather than the
    # user stated: they still surface, but a stated memory of similar
    # relevance outranks them.
    inferred_weight: float = 0.8

    # Retrieval strengthening (ACT-R): when > 0, recall score is multiplied by
    # 1 + ln(1 + use_count) / use_count_dampening. Off (0) by default:
    # use_count counts every surfacing, not actual use, so the boost feeds
    # back into itself — at the old default of 4.0, memories surfaced
    # hundreds of times (x2.6) buried clearly better matches.
    use_count_dampening: float = 0.0

    # `axiom review` consolidation report thresholds.
    stale_state_days: int = 90  # state memories untouched this long are flagged
    zombie_days: int = 60  # barely-used memories older than this are flagged
    # use_count is bumped whenever recall *surfaces* a memory, whether or not
    # the client used it, so it inflates. A memory at or below this count that
    # also hasn't surfaced within zombie_days still counts as a zombie.
    zombie_max_use_count: int = 2

    # Episodic events (log_event): weak ambient signals, never part of recall.
    # Trigram similarity for two events to count as the same pattern.
    event_similarity: float = 0.3
    # Events older than this are deleted on the next write — the episodic
    # layer forgets outright, unlike memories which only sink in ranking.
    event_retention_days: int = 180
    # `axiom review` looks for patterns within this window...
    event_window_days: int = 30
    # ...and reports clusters of at least this many similar events.
    event_min_count: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
