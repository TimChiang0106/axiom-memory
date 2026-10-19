"""Long-form documents: specs, plans, meeting notes — anything too long to be
a curated memory. Fetched by exact name only; no embedding and never part of
recall, so a 20-page spec cannot crowd out the facts recall is for. A memory
points at a document by writing `doc:<name>` in its content. Every overwrite
is snapshotted by a database trigger (migration 0011)."""

from typing import Literal

import asyncpg

from axiom.models import Document, DocumentSummary, DocumentVersion

_COLUMNS = "name, title, body, project, source_app, created_at, updated_at"


class DocumentStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def put(
        self,
        *,
        name: str,
        title: str,
        body: str,
        project: str | None,
        source_app: str,
    ) -> tuple[Literal["created", "updated"], Document]:
        row = await self._pool.fetchrow(
            f"""
            INSERT INTO documents AS d (name, title, body, project, source_app)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (name) DO UPDATE
                SET title = EXCLUDED.title, body = EXCLUDED.body, project = EXCLUDED.project,
                    source_app = EXCLUDED.source_app, updated_at = now()
            RETURNING {_COLUMNS}, (xmax = 0) AS inserted
            """,
            name,
            title,
            body,
            project,
            source_app,
        )
        assert row is not None
        doc = Document(**{k: row[k] for k in Document.model_fields})
        return ("created" if row["inserted"] else "updated"), doc

    async def get(self, name: str) -> Document | None:
        row = await self._pool.fetchrow(f"SELECT {_COLUMNS} FROM documents WHERE name = $1", name)
        return Document(**dict(row)) if row else None

    async def history(self, name: str, *, limit: int = 20) -> list[DocumentVersion]:
        rows = await self._pool.fetch(
            """
            SELECT title, body, project, source_app, written_at, superseded_at
            FROM document_versions WHERE name = $1
            ORDER BY superseded_at DESC, id DESC LIMIT $2
            """,
            name,
            limit,
        )
        return [DocumentVersion(**dict(r)) for r in rows]

    async def list_all(self, *, project: str | None = None) -> list[DocumentSummary]:
        rows = await self._pool.fetch(
            """
            SELECT name, title, project, length(body) AS chars, updated_at FROM documents
            WHERE $1::text IS NULL OR project = $1
            ORDER BY updated_at DESC
            """,
            project,
        )
        return [DocumentSummary(**dict(r)) for r in rows]
