-- Work OS: tasks live in their own table instead of the "open items" section
-- of project overviews. An overview is a memory (curated, recalled, ranked);
-- a to-do has a lifecycle (open -> done) and dates, and is read by date, not
-- by relevance — so it never enters recall.
--
-- Two sources: 'stated' (the user said it — goes straight to open) and
-- 'ai_suggested' (an AI proposed it — lands as 'suggested' and needs the
-- user's confirm). Suggestions are rationed and screened server-side (see
-- axiom.tasks) so an eager model cannot flood the list.

CREATE TABLE tasks (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title         TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    -- The `<project>-overview` name prefix this task belongs to; NULL for
    -- tasks outside any project.
    project       TEXT CHECK (project ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    status        TEXT NOT NULL
                  CHECK (status IN ('suggested', 'open', 'done', 'dropped', 'expired')),
    source        TEXT NOT NULL CHECK (source IN ('stated', 'ai_suggested')),
    -- Deadline (external) vs the day the user plans to do it (internal).
    due_date      DATE,
    planned_date  DATE,
    -- "Later, no date" — counts as arranged, unlike a task with no plan at all.
    someday       BOOLEAN NOT NULL DEFAULT false,
    -- How many times planned_date was pushed later; a task deferred again and
    -- again is probably not going to happen and gets asked about.
    defer_count   INT NOT NULL DEFAULT 0,
    -- {"trigger": ..., "memories": [names...], "note": ...}
    evidence      JSONB,
    reject_reason TEXT,
    -- Set when a suggestion is rejected; rejected suggestions from the last
    -- 30 days block look-alike suggestions.
    rejected_at   TIMESTAMPTZ,
    done_at       TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Suggestions left unanswered past this become 'expired'.
    expires_at    TIMESTAMPTZ,
    -- Title embedding for the look-alike screen; NULL falls back to trigrams.
    embedding     vector(768),
    CHECK (NOT (someday AND planned_date IS NOT NULL))
);

CREATE INDEX tasks_status_idx ON tasks (status);
CREATE INDEX tasks_planned_idx ON tasks (planned_date) WHERE status = 'open';
CREATE INDEX tasks_project_idx ON tasks (project);
CREATE INDEX tasks_title_trgm_idx ON tasks USING GIN (title gin_trgm_ops);
