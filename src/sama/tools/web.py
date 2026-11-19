"""The ``web`` toolset: ``web_search`` and ``web_extract`` (docs/architecture.md, Section 16.7)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx
from pydantic import SecretStr

from sama.tools.outbound import BlockedUrlError, FetchError, OutboundClient
from sama.tools.output import untrusted
from sama.tools.text import UnsupportedContentError, page_text

log = logging.getLogger("sama.tools")

MAX_SEARCH_RESULTS: Final = 10
MAX_EXTRACT_URLS: Final = 5
BRAVE_URL: Final = "https://api.search.brave.com/res/v1/web/search"


class SearchError(Exception):
    """The search provider failed. The message is safe to show to the agent."""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int, *, org_id: str) -> list[SearchHit]: ...


KeyLookup = Callable[[str], Awaitable[SecretStr | None]]
"""Gives the key of an org (``sama.vault.store.OrgKeys``)."""


class BraveSearch:
    """The Brave Search API. The key stays on the control plane. ``api_key`` is one key for
    all orgs, or a lookup that gives the key of each org (the vault, Section 16.3)."""

    def __init__(
        self,
        api_key: SecretStr | KeyLookup,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._client = httpx.AsyncClient(transport=transport, timeout=15.0, trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _key_of(self, org_id: str) -> SecretStr:
        if isinstance(self._key, SecretStr):
            return self._key
        try:
            key = await self._key(org_id)
        except Exception:
            log.exception("the search key of an org cannot be read org=%s", org_id)
            raise SearchError("the search key cannot be read now") from None
        if key is None:
            raise SearchError("web search has no API key for this org")
        return key

    async def search(self, query: str, limit: int, *, org_id: str) -> list[SearchHit]:
        key = await self._key_of(org_id)
        try:
            response = await self._client.get(
                BRAVE_URL,
                params={"q": query, "count": limit, "text_decorations": "false"},
                headers={
                    "accept": "application/json",
                    "x-subscription-token": key.get_secret_value(),
                },
            )
        except httpx.HTTPError as error:
            raise SearchError(f"the search failed: {type(error).__name__}") from None
        if response.status_code == 429:
            raise SearchError("the search provider limits the rate. Try again later.")
        if response.status_code != 200:
            raise SearchError(f"the search failed with status {response.status_code}")
        try:
            body: Any = response.json()
        except ValueError:
            raise SearchError("the search provider sent a reply that is not JSON") from None
        web = body.get("web") if isinstance(body, dict) else None
        results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(results, list):
            return []
        hits = [
            SearchHit(
                title=str(r.get("title", "")),
                url=str(r["url"]),
                snippet=str(r.get("description", "")),
            )
            for r in results
            if isinstance(r, dict) and r.get("url")
        ]
        return hits[:limit]


def format_hits(query: str, hits: Sequence[SearchHit]) -> str:
    if not hits:
        body = f"No results for: {query}"
    else:
        lines = [f"Results for: {query}", ""]
        for number, hit in enumerate(hits, start=1):
            lines += [f"{number}. {hit.title}", f"   {hit.url}", f"   {hit.snippet}", ""]
        body = "\n".join(lines).rstrip()
    return untrusted(body, source="web_search")


async def extract_one(client: OutboundClient, url: str) -> str:
    """The text of one page, marked as untrusted, or an error line."""
    try:
        result = await client.get(url)
        if result.status_code >= 400:
            raise FetchError(f"the server returned status {result.status_code}")
        title, text = page_text(result.content, result.content_type)
    except (BlockedUrlError, FetchError, UnsupportedContentError) as error:
        return f"[{url}] error: {error}"
    except Exception as error:  # one bad page must not fail the other pages of the call
        log.warning("web_extract failed: error=%s", type(error).__name__)
        return f"[{url}] error: the page could not be read"
    header = f"Title: {title}\n\n" if title else ""
    note = "\n\n[The page was larger than the size limit. The text ends early.]"
    body = header + text + (note if result.truncated else "")
    return untrusted(body, source="web_extract", url=result.url)


async def extract(client: OutboundClient, urls: Sequence[str]) -> str:
    pages = await asyncio.gather(*(extract_one(client, url) for url in urls))
    return "\n\n".join(pages)
