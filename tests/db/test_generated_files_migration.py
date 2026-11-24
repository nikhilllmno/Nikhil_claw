"""Migration 0053: files of ``file_send`` in ``generated_media`` (docs/architecture.md, Section
11.10; roadmap M8.6)."""

from __future__ import annotations

import asyncio

import asyncpg
from alembic import command

from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed


async def _insert(url: str, kind: str, mime: str, name: str | None) -> str | None:
    """Inserts one row in a transaction that is rolled back. Gives the failed check, if any."""
    connection = await asyncpg.connect(url)
    try:
        run = (await connection.fetch("SELECT org_id, session_id, id FROM runs LIMIT 1"))[0]
        columns = "org_id, session_id, run_id, kind, mime_type, size_bytes, sha256, data"
        values = "$1, $2, $3, $4, $5, 3, 'h', 'abc'::bytea"
        args: list[object] = [run["org_id"], run["session_id"], run["id"], kind, mime]
        if name is not None:
            columns += ", name"
            values += ", $6"
            args.append(name)
        try:
            async with connection.transaction():
                await connection.execute(
                    f"INSERT INTO generated_media ({columns}) VALUES ({values})",  # noqa: S608
                    *args,
                )
                raise _Rollback
        except _Rollback:
            return None
        except asyncpg.CheckViolationError as error:
            return error.constraint_name
    finally:
        await connection.close()


class _Rollback(Exception):
    pass


async def _name_granted(url: str) -> bool:
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT 1 FROM information_schema.column_privileges WHERE table_schema = 'public'"
            " AND table_name = 'generated_media' AND grantee = 'authenticated'"
            " AND column_name = 'name'"
        )
        return bool(rows)
    finally:
        await connection.close()


def test_the_upgrade_allows_files_with_a_name_and_the_downgrade_removes_them() -> None:
    with migrated_database() as url:
        asyncio.run(seed(url))
        config = alembic_config(url)
        assert asyncio.run(_insert(url, "file", "text/csv", "top5.csv")) is None
        assert asyncio.run(_insert(url, "image", "image/png", None)) is None
        assert asyncio.run(_insert(url, "file", "text/x-shellscript", "a.sh")) == (
            "ck_generated_media_mime_type"
        )
        assert asyncio.run(_insert(url, "video", "video/mp4", "a.mp4")) == "ck_generated_media_kind"
        assert asyncio.run(_name_granted(url)) is True
        command.downgrade(config, "0052")
        assert asyncio.run(_insert(url, "file", "text/csv", None)) == "ck_generated_media_kind"
        assert asyncio.run(_insert(url, "image", "image/png", None)) is None
        assert asyncio.run(_name_granted(url)) is False
        command.upgrade(config, "head")
