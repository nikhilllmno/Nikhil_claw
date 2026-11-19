"""Fixtures for the database tests.

The tests need a Postgres server. Set ``SAMA_TEST_DATABASE_URL`` to a URL whose user can
create databases, for example ``postgresql://postgres:sama@localhost:55432/postgres``. Each
test session creates a new database, runs the Supabase shim and the migrations, and drops the
database at the end. Without the variable the tests are skipped.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from sama.db import create_engine
from tests.db.world import World, seed

ROOT = Path(__file__).resolve().parents[2]
SHIM = Path(__file__).with_name("supabase_shim.sql")
ENV = "SAMA_TEST_DATABASE_URL"


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["configure_logging"] = False
    return config


def _with_database(url: str, name: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{name}"))


async def _execute(url: str, sql: str) -> None:
    connection = await asyncpg.connect(url)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


@contextmanager
def migrated_database() -> Iterator[str]:
    """Creates a new database with the shim and all migrations, and drops it at the end.
    Skips the test without ``SAMA_TEST_DATABASE_URL``."""
    admin_url = os.environ.get(ENV)
    if not admin_url:
        pytest.skip(f"set {ENV} to run the database tests")
    name = f"sama_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = _with_database(admin_url, name)
    try:
        asyncio.run(_execute(url, SHIM.read_text()))
        config = alembic_config(url)
        # A full round trip proves that the downgrade works.
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        yield url
    finally:
        asyncio.run(_execute(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """A new database with the shim and all migrations."""
    with migrated_database() as url:
        yield url


@pytest.fixture(scope="session")
def world(database_url: str) -> World:
    """Two orgs with users, workspaces, sessions, and child rows."""
    return asyncio.run(seed(database_url))


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()
