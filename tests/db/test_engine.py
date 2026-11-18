"""Unit tests for the engine helpers. They need no database."""

from __future__ import annotations

import pytest

from sama.db import async_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgresql+asyncpg://u@h/db", "postgresql+asyncpg://u@h/db"),
        ("postgresql://u@h/db?sslmode=require", "postgresql+asyncpg://u@h/db?ssl=require"),
    ],
)
def test_async_url_uses_asyncpg(url: str, expected: str) -> None:
    assert async_url(url) == expected


def test_async_url_rejects_other_schemes() -> None:
    with pytest.raises(ValueError, match="postgresql"):
        async_url("mysql://u@h/db")
