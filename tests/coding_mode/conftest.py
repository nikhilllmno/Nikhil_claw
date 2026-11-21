"""Fixtures for the coding mode tests. They use the database and the tenants of the queue
tests."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.coding_mode.service import CodingService
from sama.orchestrator.queue import RunQueue
from tests.orchestrator.conftest import engine, queue_database_url, tenant  # noqa: F401


@pytest.fixture
def coding(engine: AsyncEngine) -> CodingService:  # noqa: F811
    return CodingService(engine, RunQueue(engine))
