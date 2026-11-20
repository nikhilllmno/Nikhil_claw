"""A download with a hard byte cap (docs/architecture.md, Section 11.7).

The cap counts the bytes that came over the wire. The client asks for ``identity`` encoding and
refuses an answer with another ``Content-Encoding`` before it reads one byte. So no decoder runs,
the bytes in memory are the bytes on the wire, and a small compressed body cannot grow past the
cap. No chunk is added before the cap is checked.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx

TOO_LARGE = "download failed: the file is too large"


async def fetch_capped[E: Exception](
    http: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    fail: Callable[[str], E],
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """The body of ``url``. Raises ``fail(message)`` for a status other than 200, an encoded
    body, a body above ``max_bytes``, or a transport error. No redirect is followed."""
    request_headers = {**(headers or {}), "Accept-Encoding": "identity"}
    data = bytearray()
    try:
        async with http.stream(
            "GET", url, headers=request_headers, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                raise fail(f"download failed: {response.status_code}")
            encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise fail("download failed: the answer is encoded")
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise fail(TOO_LARGE)
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise fail(TOO_LARGE)
                data.extend(chunk)
    except httpx.HTTPError as error:
        raise fail(f"download failed: {type(error).__name__}") from None
    return bytes(data)
