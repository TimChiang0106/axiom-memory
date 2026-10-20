"""Test fixtures: a dedicated axiom_test database, recreated once per session,
truncated between tests. Requires the compose.yaml Postgres to be running."""

import os
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from axiom.db import create_pool, run_migrations
from axiom.documents import DocumentStore
from axiom.store import MemoryStore
from axiom.tasks import TaskStore

ADMIN_DSN = os.environ.get("AXIOM_TEST_ADMIN_DSN", "postgresql://axiom:axiom@localhost:5432/axiom")
TEST_DB = "axiom_test"
TRUNCATE = (
    "TRUNCATE memories, events, memory_versions, tasks, documents, document_versions, "
    "review_dismissals "
    "RESTART IDENTITY"
)


def _test_dsn() -> str:
    parts = urlparse(ADMIN_DSN)
    return urlunparse(parts._replace(path=f"/{TEST_DB}"))


@pytest.fixture(scope="session")
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(ADMIN_DSN)
    try:
        await admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} (FORCE)")
        await admin.execute(f"CREATE DATABASE {TEST_DB}")
    finally:
        await admin.close()

    pool = await create_pool(_test_dsn())
    await run_migrations(pool)
    yield pool
    await pool.close()


@pytest.fixture
async def store(pool: asyncpg.Pool) -> MemoryStore:
    await pool.execute(TRUNCATE)
    return MemoryStore(pool, dedup_threshold=0.35, recall_threshold=0.1)


@pytest.fixture
async def tasks(pool: asyncpg.Pool) -> TaskStore:
    await pool.execute(TRUNCATE)
    return TaskStore(pool)


@pytest.fixture
async def documents(pool: asyncpg.Pool) -> DocumentStore:
    await pool.execute(TRUNCATE)
    return DocumentStore(pool)
