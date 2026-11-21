"""``x_search``: search recent posts on X (docs/architecture.md, Section 10.2.2; roadmap M7.5).

The tool calls the recent search of the X API v2 (the posts of the last 7 days) with the bearer
token of the org, else the token of sama (``X_BEARER_TOKEN``). The token stays in the control
plane. The posts are untrusted content.

The X API charges for each post that it returns, so one call asks for at most
``MAX_RESULTS`` posts. The API returns at least 10 posts, so a smaller ``limit`` only shortens
the result.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import httpx

from sama.tenancy.run_token import RunTokenClaims
from sama.vault.crypto import VaultError
from sama.vault.redact import Redactor, default_redactor
from sama.vault.store import OrgKeys, SecretName

log = logging.getLogger("sama.tools.x")

X_API_URL: Final = "https://api.x.com"
MAX_QUERY_CHARS: Final = 512
"""The query limit of the recent search of the X API."""
MAX_RESULTS: Final = 20
API_MIN_RESULTS: Final = 10
DEFAULT_RESULTS: Final = 10
MAX_POST_CHARS: Final = 600
TIMEOUT_S: Final = 20.0
NO_KEY: Final = "X search is not available: no X API token is configured for the org."
Sort = Literal["recency", "relevancy"]


class XSearchError(Exception):
    """The search failed. The text is safe for the model: no token, no URL, no body."""


@dataclass(frozen=True, slots=True)
class Post:
    id: str
    text: str
    author: str
    created_at: str
    likes: int
    reposts: int
    replies: int

    @property
    def url(self) -> str:
        user = self.author or "i"
        return f"https://x.com/{user}/status/{self.id}"


@dataclass(frozen=True, slots=True)
class XResult:
    available: bool
    posts: tuple[Post, ...] = ()
    text: str = ""


class XSearch:
    def __init__(
        self,
        keys: OrgKeys,
        *,
        base_url: str = X_API_URL,
        http: httpx.AsyncClient | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self._keys = keys
        self._redactor = redactor or default_redactor()
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_S, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _token(self, claims: RunTokenClaims) -> str | None:
        try:
            found = await self._keys.get(claims.org_id, SecretName.X_BEARER_TOKEN)
        except VaultError:
            log.warning("the X token of org=%s does not decrypt", claims.org_id)
            return None
        return None if found is None else found.get_secret_value()

    async def search(
        self,
        claims: RunTokenClaims,
        query: str,
        *,
        limit: int = DEFAULT_RESULTS,
        sort: Sort = "recency",
    ) -> XResult:
        query = query.strip()
        if not query:
            raise XSearchError("The query is empty.")
        if len(query) > MAX_QUERY_CHARS:
            raise XSearchError(f"The query has more than {MAX_QUERY_CHARS} characters.")
        if not 1 <= limit <= MAX_RESULTS:
            raise XSearchError(f"limit must be 1 to {MAX_RESULTS}.")
        token = await self._token(claims)
        if token is None:
            return XResult(available=False, text=NO_KEY)
        params = {
            # The query leaves the control plane in the URL: redact it first.
            "query": self._redactor.text(query),
            "max_results": str(max(API_MIN_RESULTS, limit)),
            "sort_order": sort,
            "tweet.fields": "created_at,author_id,public_metrics",
            "expansions": "author_id",
            "user.fields": "username",
        }
        try:
            response = await self._http.get(
                f"{self._base_url}/2/tweets/search/recent",
                params=params,
                headers={"authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException:
            raise XSearchError("X search did not answer in time. Try again.") from None
        except httpx.HTTPError as error:
            log.warning("x_search failed run=%s error=%s", claims.run_id, type(error).__name__)
            raise XSearchError("X search cannot be reached now. Try again later.") from None
        if response.status_code != 200:
            log.warning("x_search refused run=%s status=%d", claims.run_id, response.status_code)
            raise XSearchError(_failure(response.status_code))
        try:
            data = response.json()
        except ValueError:
            raise XSearchError("X search gave an answer that sama cannot read.") from None
        posts = parse_posts(data if isinstance(data, dict) else {}, self._redactor)
        return XResult(available=True, posts=tuple(posts[:limit]))


def _int(data: Mapping[str, Any] | None, key: str) -> int:
    value = data.get(key) if data else None
    return value if isinstance(value, int) and value >= 0 else 0


def parse_posts(data: Mapping[str, Any], redactor: Redactor | None = None) -> list[Post]:
    """The posts of an answer. Each text is redacted whole, then cut."""
    redactor = redactor or default_redactor()
    includes = data.get("includes")
    users = includes.get("users") if isinstance(includes, Mapping) else None
    names = {
        str(user.get("id")): str(user.get("username", ""))
        for user in (users if isinstance(users, list) else [])
        if isinstance(user, Mapping)
    }
    posts: list[Post] = []
    for item in data.get("data") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        metrics = item.get("public_metrics")
        metrics = metrics if isinstance(metrics, Mapping) else None
        posts.append(
            Post(
                id=item["id"],
                text=redactor.text(str(item.get("text", "")))[:MAX_POST_CHARS],
                author=names.get(str(item.get("author_id")), ""),
                created_at=str(item.get("created_at", "")),
                likes=_int(metrics, "like_count"),
                reposts=_int(metrics, "retweet_count"),
                replies=_int(metrics, "reply_count"),
            )
        )
    return posts


def format_posts(query: str, posts: Sequence[Post]) -> str:
    if not posts:
        return f"No posts on X in the last 7 days match: {query}"
    lines = [f"Posts on X for: {query} (the last 7 days)"]
    for index, post in enumerate(posts, start=1):
        author = f"@{post.author}" if post.author else "unknown author"
        lines.append(
            f"{index}. {author}, {post.created_at}, {post.likes} likes, {post.reposts} reposts,"
            f" {post.replies} replies\n   {post.url}\n   {' '.join(post.text.split())}"
        )
    return "\n".join(lines)


def _failure(status: int) -> str:
    if status == 400:
        return "X search refused the query. Check the search operators and try again."
    if status in (401, 403):
        return "X search refused the token of the org. Ask an admin to check it."
    if status == 402:
        return "X search is not available: the X API account has no credits. Ask an admin."
    if status == 429:
        return "X search is busy now (rate limit). Try again later."
    return "X search is not available now. Try again later."
