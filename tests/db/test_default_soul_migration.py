"""Migration 0051: the default SOUL for default profiles whose SOUL is empty (docs/architecture.md,
Section 6.1; roadmap M8.4)."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
from alembic import command

from sama.instructions import DEFAULT_SOUL
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")

MIGRATION = Path(__file__).parents[2] / "migrations" / "versions" / "0051_default_soul.py"


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0051", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


def _fetch(url: str, query: str, *args: object) -> list[asyncpg.Record]:
    return asyncio.run(_run(url, query, *args))


def _soul(url: str, profile_id: uuid.UUID) -> str:
    [row] = _fetch(url, "SELECT soul FROM agent_profiles WHERE id = $1", profile_id)
    return str(row["soul"])


def test_the_migration_text_is_the_default_soul_of_new_profiles() -> None:
    assert _migration().SOUL == DEFAULT_SOUL


def test_the_upgrade_fills_only_empty_default_souls_and_the_downgrade_undoes_it() -> None:
    soul = _migration().SOUL
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0050")
        a, a2, b = (world.ids[name] for name in ("profile-a", "profile-a2", "profile-b"))
        # A person wrote the SOUL of org B: it must not change.
        _fetch(url, "UPDATE agent_profiles SET soul = 'Custom soul.' WHERE id = $1", b)
        # A second org-wide profile of org A, newer than the default profile.
        [later] = _fetch(
            url,
            "INSERT INTO agent_profiles (org_id, name, model, created_at)"
            " SELECT org_id, 'later', model, created_at + interval '1 hour'"
            " FROM agent_profiles WHERE id = $1 RETURNING id",
            a,
        )
        command.upgrade(config, "head")

        assert _soul(url, a) == soul
        assert _soul(url, a2) == ""  # a profile of a workspace is not a default profile
        assert _soul(url, b) == "Custom soul."
        assert _soul(url, later["id"]) == ""

        [change] = _fetch(
            url,
            "SELECT kind, source, before, after, actor_user_id, summary FROM profile_changes"
            " WHERE agent_profile_id = $1 AND summary IS NOT NULL",
            a,
        )
        assert (change["kind"], change["source"], change["actor_user_id"]) == (
            "soul",
            "edit",
            None,
        )
        assert '"soul": ""' in change["before"]
        assert soul[:40] in change["after"]
        [audit] = _fetch(
            url,
            "SELECT actor_kind, details FROM audit_log"
            " WHERE action = 'profile.updated' AND target_id = $1",
            str(a),
        )
        assert audit["actor_kind"] == "system"
        assert "migration 0051" in audit["details"]

        command.downgrade(config, "0050")
        assert _soul(url, a) == ""
        assert _soul(url, b) == "Custom soul."
        assert not _fetch(
            url, "SELECT 1 FROM profile_changes WHERE summary = $1", _migration().SUMMARY
        )


def test_the_downgrade_keeps_a_soul_that_a_person_changed_after_the_fill() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0050")
        command.upgrade(config, "head")
        a = world.ids["profile-a"]
        assert _soul(url, a) == _migration().SOUL
        _fetch(url, "UPDATE agent_profiles SET soul = 'Edited.' WHERE id = $1", a)
        command.downgrade(config, "0050")
        assert _soul(url, a) == "Edited."
