-- Long-form documents (specs, meeting notes, plans) that are too long to be a
-- memory. Fetched by name only: no embedding, never part of recall. A memory
-- points at one with `doc:<name>` in its content. Versioned by trigger, the
-- same way memories are (migration 0008), so an overwrite stays recoverable.

CREATE TABLE documents (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]{1,63}$'),
    title       TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    body        TEXT NOT NULL,
    project     TEXT CHECK (project ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    source_app  TEXT NOT NULL DEFAULT 'unknown',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document_versions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    project        TEXT,
    source_app     TEXT NOT NULL,
    written_at     TIMESTAMPTZ NOT NULL,
    superseded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX document_versions_name_idx ON document_versions (name, superseded_at DESC);

CREATE FUNCTION documents_snapshot() RETURNS trigger AS $$
BEGIN
    INSERT INTO document_versions (name, title, body, project, source_app, written_at)
    VALUES (OLD.name, OLD.title, OLD.body, OLD.project, OLD.source_app, OLD.updated_at);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER documents_snapshot_on_update
    AFTER UPDATE ON documents
    FOR EACH ROW
    WHEN (OLD.title IS DISTINCT FROM NEW.title
          OR OLD.body IS DISTINCT FROM NEW.body
          OR OLD.project IS DISTINCT FROM NEW.project)
    EXECUTE FUNCTION documents_snapshot();
