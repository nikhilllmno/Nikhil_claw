"""Session search and session read with a database (Section 7.1)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import EMBEDDING_DIMENSIONS, Membership, Message
from sama.search import EmbedError, EmbedWorker, SessionHit, SessionSearch, SessionSearchError
from sama.search.core import HEADLINE_SOURCE_CHARS
from sama.vault.redact import default_redactor
from tests.search.rig import World, say

AXES = {"pgbouncer": 0, "pooling": 0, "pooler": 0, "deploy": 1, "fly": 1}


class Topics:
    """A fake embedder. A text has a vector on the axis of each topic word that it has. The
    topics stand for the meaning of the text."""

    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.delay = 0.0
        self.calls = 0
        self.texts: list[str] = []
        self.before: Callable[[], Awaitable[None]] | None = None

    async def embed(self, org_id: uuid.UUID, texts: Sequence[str]) -> list[list[float]] | None:
        self.calls += 1
        self.texts.extend(texts)
        if self.before is not None:
            await self.before()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        vectors = []
        for text in texts:
            vector = [0.0] * EMBEDDING_DIMENSIONS
            for word, axis in AXES.items():
                if word in text.lower():
                    vector[axis] = 1.0
            if not any(vector):
                vector[2] = 1.0
            vectors.append(vector)
        return vectors


def titles(hits: Sequence[SessionHit]) -> list[str | None]:
    return [hit.title for hit in hits]


@pytest.fixture
async def talk(engine: AsyncEngine, world: World) -> World:
    """Sessions with text. Each of them has the word ``pgbouncer`` in the second message."""
    ids = world.ids
    org = world.org
    assert world.other is not None
    await say(engine, org, ids["s1"], "How do we deploy to fly?")
    await say(
        engine, org, ids["s1"], "Use fly deploy with the blue green strategy", role="assistant"
    )
    await say(engine, org, ids["s2"], "pgbouncer in transaction mode breaks prepared statements")
    await say(engine, org, ids["mine"], "We chose pgbouncer for connection pooling")
    await say(engine, org, ids["bobs"], "pgbouncer: the private plan of bob")
    await say(engine, org, ids["other_ws"], "pgbouncer in the other workspace")
    await say(engine, world.other.org, ids["other_org"], "pgbouncer in the other org")
    await say(engine, org, ids["s3"], "pgbouncer in the current session")
    return world


async def test_the_search_finds_words_and_ranks_the_best_message_first(
    engine: AsyncEngine, talk: World
) -> None:
    search = SessionSearch(engine)
    hits = await search.search(talk.claims(), "pgbouncer prepared statements", 5)
    assert hits[0].session_id == talk.ids["s2"]
    assert hits[0].snippets[0].text.count("<<") >= 2
    assert "<<pgbouncer>>" in hits[0].snippets[0].text


async def test_a_private_run_sees_shared_sessions_and_its_own_private_sessions(
    engine: AsyncEngine, talk: World
) -> None:
    hits = await SessionSearch(engine).search(talk.claims(), "pgbouncer", 10)
    # Not the current session (s3), not bob's private session, not the other workspace, not the
    # other org.
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["mine"]}


async def test_the_search_does_not_list_the_current_session(
    engine: AsyncEngine, talk: World
) -> None:
    hits = await SessionSearch(engine).search(talk.claims("mine"), "pgbouncer", 10)
    assert talk.ids["mine"] not in {h.session_id for h in hits}
    assert talk.ids["s3"] in {h.session_id for h in hits}


async def test_a_shared_run_sees_only_shared_sessions_of_its_workspace(
    engine: AsyncEngine, talk: World
) -> None:
    claims = talk.claims("s1", user=None)
    hits = await SessionSearch(engine).search(claims, "pgbouncer", 10)
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["s3"]}
    assert {h.audience for h in hits} == {"shared"}


async def test_each_user_sees_the_sessions_of_the_own_workspaces(
    engine: AsyncEngine, talk: World
) -> None:
    search = SessionSearch(engine)
    ids = talk.ids
    bob = await search.search(talk.claims("s1", user="bob"), "pgbouncer", 10)
    assert {h.session_id for h in bob} == {ids["s2"], ids["s3"], ids["bobs"]}
    cat = await search.search(talk.claims("other_ws", user="cat", workspace="ws2"), "pgbouncer", 10)
    assert {h.session_id for h in cat} == set()
    # An org admin sees the shared sessions of every workspace, and no private session of
    # another user.
    ada = await search.search(talk.claims("s1", user="ada"), "pgbouncer", 10)
    assert {h.session_id for h in ada} == {ids["s2"], ids["s3"], ids["other_ws"]}


async def test_a_user_without_membership_sees_nothing(engine: AsyncEngine, talk: World) -> None:
    search = SessionSearch(engine)
    assert await search.search(talk.claims("s1", user="eve"), "pgbouncer", 10) == []
    assert await search.search(talk.claims("s1", user="eve"), None, 10) == []
    with pytest.raises(SessionSearchError, match="session not found"):
        await search.read(talk.claims("s1", user="eve"), str(talk.ids["s2"]), 0, 10)


async def test_an_empty_query_lists_the_latest_sessions(engine: AsyncEngine, talk: World) -> None:
    search = SessionSearch(engine)
    hits = await search.search(talk.claims(), None, 10)
    ids = {h.session_id for h in hits}
    assert ids == {talk.ids[s] for s in ("s1", "s2", "mine")}
    assert all(not h.snippets for h in hits)
    assert await search.search(talk.claims(), "   ", 10) == hits
    assert len(await search.search(talk.claims(), None, 1)) == 1


async def test_the_limit_is_kept_and_a_query_without_a_match_is_empty(
    engine: AsyncEngine, talk: World
) -> None:
    search = SessionSearch(engine)
    assert len(await search.search(talk.claims(), "pgbouncer", 1)) == 1
    assert await search.search(talk.claims(), "zebra", 5) == []


async def test_a_session_gives_at_most_two_snippets(engine: AsyncEngine, talk: World) -> None:
    for number in range(4):
        await say(engine, talk.org, talk.ids["s2"], f"pgbouncer note {number}")
    hits = await SessionSearch(engine).search(talk.claims(), "pgbouncer", 5)
    by_session = {h.session_id: h for h in hits}
    assert len(by_session[talk.ids["s2"]].snippets) == 2


async def test_a_long_query_is_refused(engine: AsyncEngine, talk: World) -> None:
    with pytest.raises(SessionSearchError, match="400"):
        await SessionSearch(engine).search(talk.claims(), "a" * 401, 5)


async def test_special_characters_in_the_query_are_safe(engine: AsyncEngine, talk: World) -> None:
    search = SessionSearch(engine)
    for query in ("'; DROP TABLE messages; --", "a & | ! ( ) :*", '"unclosed', "-", "\\"):
        await search.search(talk.claims(), query, 5)


async def test_snippets_and_titles_are_redacted(engine: AsyncEngine, talk: World) -> None:
    secret = "zq-redact-me-7781"
    default_redactor().add(secret)
    try:
        await say(engine, talk.org, talk.ids["s1"], f"the token is {secret} for pgbouncer")
        hits = await SessionSearch(engine).search(talk.claims(), "token pgbouncer", 5)
        page = await SessionSearch(engine).read(talk.claims(), str(talk.ids["s1"]), 0, 10)
    finally:
        default_redactor().forget(secret)
    shown = " ".join(s.text for h in hits for s in h.snippets)
    assert "token" in shown
    assert secret not in shown
    assert secret not in " ".join(m.text for m in page.messages)


# -- read -----------------------------------------------------------------------------------


async def test_read_gives_a_page_of_messages_in_order(engine: AsyncEngine, talk: World) -> None:
    ids = talk.ids
    for number in range(5):
        await say(engine, talk.org, ids["s2"], f"line {number}")
    search = SessionSearch(engine)
    first = await search.read(talk.claims(), str(ids["s2"]), 0, 4)
    assert first.total == 6
    assert [m.text for m in first.messages][:2] == [
        "pgbouncer in transaction mode breaks prepared statements",
        "line 0",
    ]
    assert [m.seq for m in first.messages] == sorted(m.seq for m in first.messages)
    second = await search.read(talk.claims(), str(ids["s2"]), 4, 4)
    assert [m.text for m in second.messages] == ["line 3", "line 4"]
    assert second.offset == 4
    assert (await search.read(talk.claims(), str(ids["s2"]), 100, 4)).messages == ()


async def test_read_skips_system_and_tool_messages(engine: AsyncEngine, talk: World) -> None:
    await say(engine, talk.org, talk.ids["s2"], "internal", role="system")
    await say(engine, talk.org, talk.ids["s2"], "tool output", role="tool")
    page = await SessionSearch(engine).read(talk.claims(), str(talk.ids["s2"]), 0, 50)
    assert page.total == 1


async def test_read_gives_the_same_error_for_a_hidden_session_and_a_missing_one(
    engine: AsyncEngine, talk: World
) -> None:
    search = SessionSearch(engine)
    ids = talk.ids
    errors = []
    for wanted in (
        ids["bobs"],  # private, another user
        ids["other_ws"],  # a workspace that the user does not see
        ids["other_org"],  # another org
        uuid.uuid4(),  # does not exist
        "not-a-uuid",
    ):
        with pytest.raises(SessionSearchError) as error:
            await search.read(talk.claims(), str(wanted), 0, 10)
        errors.append(str(error.value))
    assert set(errors) == {"session not found"}


async def test_a_shared_run_cannot_read_a_private_session(engine: AsyncEngine, talk: World) -> None:
    with pytest.raises(SessionSearchError, match="session not found"):
        await SessionSearch(engine).read(talk.claims("s1", user=None), str(talk.ids["mine"]), 0, 10)


async def test_the_current_session_can_be_read(engine: AsyncEngine, talk: World) -> None:
    page = await SessionSearch(engine).read(talk.claims(), str(talk.ids["s3"]), 0, 10)
    assert page.total == 1


# -- meaning search ---------------------------------------------------------------------------


async def first_message(engine: AsyncEngine, session: uuid.UUID) -> uuid.UUID:
    async with engine.connect() as connection:
        row = await connection.execute(
            select(Message.id).where(Message.session_id == session).order_by(Message.seq).limit(1)
        )
        return row.scalar_one()


async def embed_all(engine: AsyncEngine, topics: Topics) -> None:
    worker = EmbedWorker(engine, topics)
    while await worker.run_once():
        pass


async def test_the_meaning_list_finds_a_message_without_the_query_words(
    engine: AsyncEngine, talk: World
) -> None:
    topics = Topics()
    search = SessionSearch(engine, topics)
    await embed_all(engine, topics)
    # No message has the word "pooler". The meaning list gives the messages on its axis.
    hits = await search.search(talk.claims(), "pooler", 5)
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["mine"]}
    # A vector hit still has a snippet: the start of the message.
    assert all(h.snippets and h.snippets[0].text for h in hits)
    without = await SessionSearch(engine).search(talk.claims(), "pooler", 5)
    assert without == []


async def test_the_meaning_list_keeps_the_visibility_rule(engine: AsyncEngine, talk: World) -> None:
    topics = Topics()
    await embed_all(engine, topics)
    search = SessionSearch(engine, topics)
    hits = await search.search(talk.claims("s1", user=None), "pooler", 10)
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["s3"]}
    hits = await search.search(talk.claims(), "pooler", 10)
    assert talk.ids["bobs"] not in {h.session_id for h in hits}
    assert talk.ids["other_org"] not in {h.session_id for h in hits}


async def test_far_messages_are_not_a_meaning_match(engine: AsyncEngine, talk: World) -> None:
    topics = Topics()
    await embed_all(engine, topics)
    hits = await SessionSearch(engine, topics).search(talk.claims(), "zebra", 5)
    assert hits == []


async def test_both_lists_give_one_result(engine: AsyncEngine, talk: World) -> None:
    topics = Topics()
    await embed_all(engine, topics)
    hits = await SessionSearch(engine, topics).search(talk.claims(), "pgbouncer", 5)
    assert sorted(h.session_id for h in hits) == sorted([talk.ids["s2"], talk.ids["mine"]])
    assert len({h.session_id for h in hits}) == len(hits)


@pytest.mark.parametrize("failure", [EmbedError("down"), None], ids=["error", "slow"])
async def test_the_search_falls_back_to_full_text(
    engine: AsyncEngine, talk: World, failure: Exception | None
) -> None:
    topics = Topics()
    await embed_all(engine, topics)
    topics.fail = failure
    if failure is None:
        topics.delay = 1.0
    search = SessionSearch(engine, topics, embed_timeout_s=0.05)
    hits = await search.search(talk.claims(), "pgbouncer", 5)
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["mine"]}


async def test_an_org_without_embeddings_makes_no_embedding_call(
    engine: AsyncEngine, talk: World
) -> None:
    topics = Topics()
    hits = await SessionSearch(engine, topics).search(talk.claims(), "pgbouncer", 5)
    assert {h.session_id for h in hits} == {talk.ids["s2"], talk.ids["mine"]}
    assert topics.calls == 0


# -- redaction order ------------------------------------------------------------------------

SECRET = "zq-redact-me-7781"
PATTERN_SECRET = "sk-" + "a1B2c3D4e5" * 3


@pytest.mark.parametrize(
    ("secret", "query"),
    [(SECRET, "redact"), (SECRET, "7781"), (PATTERN_SECRET, "a1b2c3d4e5a1b2c3d4e5a1b2c3d4e5")],
    ids=["known part", "known number", "pattern part"],
)
async def test_a_search_for_a_part_of_a_secret_does_not_show_the_secret(
    engine: AsyncEngine, talk: World, secret: str, query: str
) -> None:
    default_redactor().add(SECRET)
    try:
        await say(engine, talk.org, talk.ids["s1"], f"the token is {secret} for the deploy")
        hits = await SessionSearch(engine).search(talk.claims(), query, 5)
    finally:
        default_redactor().forget(SECRET)
    shown = " ".join(s.text for h in hits for s in h.snippets)
    assert shown, "the message must be found"
    for part in ("zq-", "me-7781", "redact-me", "sk-", "a1B2c3D4e5"):
        assert part not in shown
    assert "[REDACTED]" in shown


@pytest.mark.parametrize("secret", [SECRET, PATTERN_SECRET], ids=["known", "pattern"])
async def test_a_cut_of_the_source_text_does_not_split_a_secret(
    engine: AsyncEngine, talk: World, secret: str
) -> None:
    default_redactor().add(SECRET)
    try:
        # The secret starts before character 20,000 and ends after it.
        body = "a " * 9_990 + "needle " + secret + " tail"
        assert body.index(secret) < HEADLINE_SOURCE_CHARS < body.index(secret) + len(secret)
        await say(engine, talk.org, talk.ids["s1"], body)
        hits = await SessionSearch(engine).search(talk.claims(), "needle", 5)
    finally:
        default_redactor().forget(SECRET)
    shown = " ".join(s.text for h in hits for s in h.snippets)
    assert shown
    assert secret[:6] not in shown
    assert secret[-6:] not in shown


async def test_the_query_is_redacted_before_the_embedding(engine: AsyncEngine, talk: World) -> None:
    topics = Topics()
    await embed_all(engine, topics)
    topics.texts.clear()
    default_redactor().add(SECRET)
    try:
        await SessionSearch(engine, topics).search(talk.claims(), f"pgbouncer {SECRET}", 5)
    finally:
        default_redactor().forget(SECRET)
    assert topics.texts == ["pgbouncer [REDACTED]"]


# -- fresh grants ---------------------------------------------------------------------------


async def test_a_grant_that_ends_during_the_embedding_hides_the_session(
    engine: AsyncEngine, talk: World
) -> None:
    ids = talk.ids
    topics = Topics()
    await embed_all(engine, topics)
    async with engine.begin() as connection:
        await connection.execute(
            insert(Membership).values(
                org_id=talk.org, user_id=ids["mia"], workspace_id=ids["ws2"], role="member"
            )
        )
    search = SessionSearch(engine, topics)
    before = await search.search(talk.claims(), "pgbouncer", 10)
    assert ids["other_ws"] in {h.session_id for h in before}

    async def revoke() -> None:
        async with engine.begin() as connection:
            await connection.execute(
                delete(Membership).where(
                    Membership.user_id == ids["mia"], Membership.workspace_id == ids["ws2"]
                )
            )

    topics.before = revoke
    after = await search.search(talk.claims(), "pgbouncer", 10)
    shown = " ".join([h.title or "" for h in after] + [s.text for h in after for s in h.snippets])
    assert ids["other_ws"] not in {h.session_id for h in after}
    assert "other workspace" not in shown
    # The access to the own workspace stays.
    assert {h.session_id for h in after} >= {ids["s2"], ids["mine"]}


async def test_hydration_applies_the_rule_to_each_session_and_message(
    engine: AsyncEngine, talk: World
) -> None:
    ids = talk.ids
    search = SessionSearch(engine)
    async with engine.connect() as connection:
        visible = await search.visible(connection, talk.claims())
        # A private session of another user and a session of another workspace are chosen. The
        # rule drops them although the ids are in the list.
        chosen = [
            (ids["bobs"], [await first_message(engine, ids["bobs"])]),
            (ids["other_ws"], [await first_message(engine, ids["other_ws"])]),
            (ids["s2"], [await first_message(engine, ids["s2"])]),
        ]
        hits = await search._hits(connection, visible, func.to_tsquery("simple", "x"), chosen)
    assert [h.session_id for h in hits] == [ids["s2"]]
