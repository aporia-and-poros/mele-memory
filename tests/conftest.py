"""Each test session creates a throwaway Postgres database and drops it afterwards; tables are emptied before every test.

Needs a local Postgres with pgvector (e.g. `brew services start postgresql@17`).
The admin connection defaults to the local `postgres` database; override with MEMORY_TEST_ADMIN_DSN.
"""
import asyncio
import os
import uuid

import asyncpg
import pytest

from memory.db import apply_schema, create_pool

ADMIN_DSN = os.environ.get("MEMORY_TEST_ADMIN_DSN", "postgresql://localhost/postgres")


def _dsn_for(dbname: str) -> str:
    base = ADMIN_DSN.rsplit("/", 1)[0]
    return f"{base}/{dbname}"


@pytest.fixture(scope="session")
def test_dsn():
    dbname = f"mele_memory_test_{uuid.uuid4().hex[:10]}"

    async def _create():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(f'CREATE DATABASE "{dbname}"')
        finally:
            await conn.close()
        await apply_schema(_dsn_for(dbname))

    async def _drop():
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await conn.close()

    asyncio.run(_create())
    yield _dsn_for(dbname)
    asyncio.run(_drop())


@pytest.fixture
async def pool(test_dsn):
    p = await create_pool(test_dsn, min_size=1, max_size=4)
    await p.execute("TRUNCATE memories, sticky_notes, far_dates, letters RESTART IDENTITY CASCADE")
    yield p
    await p.close()


@pytest.fixture
def user_a():
    return uuid.uuid4()


@pytest.fixture
def user_b():
    return uuid.uuid4()
