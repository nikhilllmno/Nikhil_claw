"""The raw body of a webhook request with a size cap (docs/architecture.md, Section 11.6).

A signature covers the bytes as the provider sent them, so the hubs read the raw body. The
reader stops at the cap also when the request has no ``Content-Length`` (a chunked body).
"""

from __future__ import annotations

from fastapi import HTTPException, Request


async def read_body(request: Request, max_bytes: int) -> bytes:
    """The raw body, or ``413`` when it is larger than ``max_bytes``."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > max_bytes:
        raise HTTPException(status_code=413, detail="the body is too large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(status_code=413, detail="the body is too large")
        chunks.append(chunk)
    return b"".join(chunks)
