"""``x_search`` (docs/architecture.md, Section 10.2.2; roadmap M7.5), with a fake X API."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from sama.tenancy.run_token import RunTokenClaims
from sama.tools.x import MAX_RESULTS, XSearch, XSearchError, format_posts, parse_posts
from sama.vault.store import OrgKeys, SecretName
from tests.media.support import OwnKeys

TOKEN = "x-bearer-test-0001"
ANSWER: dict[str, Any] = {
    "data": [
        {
            "id": "111",
            "text": "sama ships   video analysis",
            "author_id": "9",
            "created_at": "2026-11-21T10:00:00.000Z",
            "public_metrics": {"like_count": 5, "retweet_count": 2, "reply_count": 1},
        },
        {"id": "222", "text": "no author", "author_id": "8"},
        {"text": "no id"},
    ],
    "includes": {"users": [{"id": "9", "username": "sama_ai"}]},
}


def claims() -> RunTokenClaims:
    return RunTokenClaims(
        org_id=str(uuid.UUID(int=1)),
        workspace_id=str(uuid.UUID(int=2)),
        session_id=str(uuid.UUID(int=3)),
        run_id=str(uuid.UUID(int=4)),
        token_id="test",
        expires_at=2_000_000_000,
    )


def build(
    status: int = 200, *, keys: OrgKeys | None = None, error: Exception | None = None
) -> tuple[XSearch, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if error is not None:
            raise error
        if status != 200:
            return httpx.Response(status, json={"title": f"bad {TOKEN}"})
        return httpx.Response(200, json=ANSWER)

    search = XSearch(
        keys or OrgKeys(None, {SecretName.X_BEARER_TOKEN: SecretStr(TOKEN)}),
        base_url="https://x.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return search, seen


async def test_a_search_sends_the_query_and_parses_the_posts() -> None:
    search, seen = build()
    result = await search.search(claims(), "sama lang:en", limit=5, sort="relevancy")
    assert result.available
    [request] = seen
    assert request.url.path == "/2/tweets/search/recent"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    params = request.url.params
    assert params["query"] == "sama lang:en"
    # The API returns at least 10 posts.
    assert params["max_results"] == "10"
    assert params["sort_order"] == "relevancy"
    assert [post.id for post in result.posts] == ["111", "222"]
    first = result.posts[0]
    assert (first.author, first.likes, first.reposts, first.replies) == ("sama_ai", 5, 2, 1)
    assert first.url == "https://x.com/sama_ai/status/111"
    assert result.posts[1].url == "https://x.com/i/status/222"
    text = format_posts("sama", result.posts)
    assert "@sama_ai" in text
    assert "sama ships video analysis" in text


async def test_the_limit_cuts_the_result() -> None:
    search, _ = build()
    result = await search.search(claims(), "sama", limit=1)
    assert len(result.posts) == 1


async def test_without_a_token_the_tool_says_it_is_off() -> None:
    search, seen = build(keys=OrgKeys(None, {}))
    result = await search.search(claims(), "sama")
    assert not result.available
    assert "no X API token" in result.text
    assert seen == []


async def test_the_token_of_the_org_comes_first() -> None:
    search, seen = build(keys=OwnKeys(SecretName.X_BEARER_TOKEN, "own-x-token"))
    await search.search(claims(), "sama")
    assert seen[0].headers["authorization"] == "Bearer own-x-token"


@pytest.mark.parametrize(
    ("status", "text"),
    [
        (400, "refused the query"),
        (401, "refused the token"),
        (402, "no credits"),
        (429, "busy"),
        (503, "not available now"),
    ],
)
async def test_errors_are_short_and_safe(status: int, text: str) -> None:
    search, _ = build(status)
    with pytest.raises(XSearchError, match=text) as caught:
        await search.search(claims(), "sama")
    assert TOKEN not in str(caught.value)


async def test_a_network_error_is_safe() -> None:
    search, _ = build(error=httpx.ConnectError(f"down {TOKEN}"))
    with pytest.raises(XSearchError, match="cannot be reached") as caught:
        await search.search(claims(), "sama")
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(("query", "limit"), [("", 5), ("x" * 513, 5), ("ok", 0), ("ok", 21)])
async def test_bad_arguments_are_refused(query: str, limit: int) -> None:
    search, seen = build()
    with pytest.raises(XSearchError):
        await search.search(claims(), query, limit=limit)
    assert seen == []
    assert MAX_RESULTS == 20


def test_no_posts() -> None:
    assert parse_posts({}) == []
    assert format_posts("q", []).startswith("No posts on X")
