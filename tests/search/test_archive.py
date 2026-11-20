"""Session search and read with archives of compactions (Sections 6.3 and 7.1)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import Session
from sama.search import SessionSearch, SessionSearchError
from sama.tools.sessions import format_page, format_sessions
from tests.search.rig import World, say


async def add_archive(
    engine: AsyncEngine,
    world: World,
    parent: str,
    *,
    audience: str = "shared",
    owner: str | None = None,
) -> uuid.UUID:
    archive = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=archive,
                org_id=world.org,
                workspace_id=world.ids["ws"],
                agent_profile_id=world.ids["profile"],
                audience=audience,
                owner_user_id=world.ids[owner] if owner else None,
                parent_session_id=world.ids[parent],
                lineage="compaction",
                status="archived",
                title="Before compaction 1: old plan",
            )
        )
    await say(engine, world.org, archive, "zebra crossing plan", role="user")
    await say(engine, world.org, archive, "[tool call Bash] {}", role="assistant")
    await say(engine, world.org, archive, "zebra output of the tool", role="tool")
    await say(engine, world.org, archive, "[2 entries are not in this archive]", role="system")
    return archive


async def test_the_search_skips_archives(engine: AsyncEngine, world: World) -> None:
    archive = await add_archive(engine, world, "s2")
    await say(engine, world.org, world.ids["s2"], "zebra in the live session")
    search = SessionSearch(engine)
    hits = await search.search(world.claims(), "zebra", 10)
    assert {h.session_id for h in hits} == {world.ids["s2"]}
    assert archive not in {h.session_id for h in hits}
    recent = await search.search(world.claims(), None, 20)
    assert archive not in {h.session_id for h in recent}


async def test_an_archive_can_be_read_with_tool_and_system_entries(
    engine: AsyncEngine, world: World
) -> None:
    archive = await add_archive(engine, world, "s2")
    page = await SessionSearch(engine).read(world.claims(), str(archive), 0, 50)
    assert page.total == 4
    assert [m.role for m in page.messages] == ["user", "assistant", "tool", "system"]
    assert (page.session.lineage, page.session.parent_session_id) == ("compaction", world.ids["s2"])
    text = format_page(page)
    assert f"compaction of session: {world.ids['s2']}" in text
    assert "zebra output of the tool" in text


async def test_the_live_session_names_its_archives(engine: AsyncEngine, world: World) -> None:
    first = await add_archive(engine, world, "s2")
    second = await add_archive(engine, world, "s2")
    await say(engine, world.org, world.ids["s2"], "live message")
    page = await SessionSearch(engine).read(world.claims(), str(world.ids["s2"]), 0, 50)
    assert page.total == 1  # the live session shows only user and assistant messages
    assert page.archives == (first, second)
    assert str(first) in format_page(page)


async def test_an_archive_follows_the_audience_of_its_session(
    engine: AsyncEngine, world: World
) -> None:
    ids = world.ids
    private = await add_archive(engine, world, "bobs", audience="private", owner="bob")
    search = SessionSearch(engine)
    # Mia is in the workspace, but the session is private to Bob. So is its archive.
    with pytest.raises(SessionSearchError):
        await search.read(world.claims(), str(private), 0, 10)
    page = await search.read(world.claims("s3", user="bob"), str(private), 0, 10)
    assert page.total == 4
    # A shared run never reads a private archive, and a hidden archive is not in the list of
    # the archives of the live session.
    with pytest.raises(SessionSearchError):
        await search.read(world.claims("s3", user=None), str(private), 0, 10)
    live = await search.read(world.claims("s3", user="bob"), str(ids["bobs"]), 0, 10)
    assert live.archives == (private,)


async def test_the_list_text_shows_a_branch(engine: AsyncEngine, world: World) -> None:
    child = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Session).values(
                id=child,
                org_id=world.org,
                workspace_id=world.ids["ws"],
                agent_profile_id=world.ids["profile"],
                audience="shared",
                parent_session_id=world.ids["s1"],
                lineage="branch",
                title="child",
            )
        )
    await say(engine, world.org, child, "kiwi")
    hits = await SessionSearch(engine).search(world.claims(), "kiwi", 5)
    assert f"branch of session: {world.ids['s1']}" in format_sessions(hits, query="kiwi")
