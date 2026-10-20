"""Connection pool and a minimal forward-only SQL migration runner."""

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_FILE = re.compile(r"^(\d{4})_.+\.sql$")


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=5)


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply pending migrations in version order, each in its own transaction."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied: set[int] = {
            r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            match = _MIGRATION_FILE.match(path.name)
            if match is None:
                raise ValueError(f"Migration filename must look like NNNN_name.sql: {path.name}")
            version = int(match.group(1))
            if version in applied:
                continue
            async with conn.transaction():
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
            logger.info("Applied migration %s", path.name)
