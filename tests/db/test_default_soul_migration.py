"""Migration 0052: the default SOUL and reasoning effort for default profiles without them
(docs/architecture.md, Section 6.1; roadmap M8.5)."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType

import asyncpg
import pytest
from alembic import command

from sama.admin.bootstrap import DEFAULT_REASONING_EFFORT
from sama.instructions import DEFAULT_SOUL
from tests.db.conftest import alembic_config, migrated_database
from tests.db.world import seed

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")

MIGRATION = (
    Path(__file__).parents[2] / "migrations" / "versions" / "0052_default_soul_and_effort.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0052", MIGRATION)
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


def _effort(url: str, profile_id: uuid.UUID) -> str | None:
    [row] = _fetch(url, "SELECT reasoning_effort FROM agent_profiles WHERE id = $1", profile_id)
    effort = row["reasoning_effort"]
    return None if effort is None else str(effort)


def test_the_migration_values_are_the_defaults_of_new_profiles() -> None:
    assert _migration().SOUL == DEFAULT_SOUL
    assert _migration().EFFORT == DEFAULT_REASONING_EFFORT


def test_the_upgrade_fills_only_empty_default_souls_and_the_downgrade_undoes_it() -> None:
    soul = _migration().SOUL
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0051")
        a, a2, b = (world.ids[name] for name in ("profile-a", "profile-a2", "profile-b"))
        # A person wrote the SOUL of org B and chose its effort: they must not change.
        _fetch(
            url,
            "UPDATE agent_profiles SET soul = 'Custom soul.', reasoning_effort = 'low'"
            " WHERE id = $1",
            b,
        )
        _fetch(url, "UPDATE agent_profiles SET reasoning_effort = NULL WHERE id <> $1", b)
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
        # The reasoning effort: only a default profile without a value gets "high".
        assert _effort(url, a) == "high"
        assert _effort(url, b) == "low"
        assert _effort(url, a2) is None
        assert _effort(url, later["id"]) is None
        [effort] = _fetch(
            url,
            "SELECT kind, before, after FROM profile_changes WHERE agent_profile_id = $1"
            " AND summary = $2",
            a,
            _migration().EFFORT_SUMMARY,
        )
        assert effort["kind"] == "profile"
        assert '"reasoning_effort": null' in effort["before"]
        assert '"reasoning_effort": "high"' in effort["after"]

        [change] = _fetch(
            url,
            "SELECT kind, source, before, after, actor_user_id, summary FROM profile_changes"
            " WHERE agent_profile_id = $1 AND summary = $2",
            a,
            _migration().SUMMARY,
        )
        assert (change["kind"], change["source"], change["actor_user_id"]) == (
            "soul",
            "edit",
            None,
        )
        assert '"soul": ""' in change["before"]
        assert soul[:40] in change["after"]
        audits = _fetch(
            url,
            "SELECT actor_kind, details FROM audit_log"
            " WHERE action = 'profile.updated' AND target_id = $1",
            str(a),
        )
        assert [row["actor_kind"] for row in audits] == ["system", "system"]
        assert all("migration 0052" in row["details"] for row in audits)

        command.downgrade(config, "0051")
        assert _soul(url, a) == ""
        assert _effort(url, a) is None
        assert _soul(url, b) == "Custom soul."
        assert _effort(url, b) == "low"
        assert not _fetch(
            url, "SELECT 1 FROM profile_changes WHERE summary = $1", _migration().SUMMARY
        )


def test_the_downgrade_keeps_a_soul_that_a_person_changed_after_the_fill() -> None:
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0051")
        command.upgrade(config, "head")
        a = world.ids["profile-a"]
        assert _soul(url, a) == _migration().SOUL
        _fetch(url, "UPDATE agent_profiles SET soul = 'Edited.' WHERE id = $1", a)
        command.downgrade(config, "0051")
        assert _soul(url, a) == "Edited."


def test_the_downgrade_keeps_a_value_that_a_person_changed_away_and_back() -> None:
    """A later change in the history of the field is a choice of a person, also when the value is
    the filled value again. A revert of the fill is a choice too."""
    with migrated_database() as url:
        world = asyncio.run(seed(url))
        config = alembic_config(url)
        command.downgrade(config, "0051")
        command.upgrade(config, "head")
        a = world.ids["profile-a"]
        for before, after in (("high", "low"), ("low", "high")):
            _fetch(
                url,
                "INSERT INTO profile_changes (org_id, agent_profile_id, kind, source, before,"
                " after, created_at) SELECT org_id, id, 'profile', 'edit',"
                " jsonb_build_object('reasoning_effort', $2::text),"
                " jsonb_build_object('reasoning_effort', $3::text), now() + interval '1 minute'"
                " FROM agent_profiles WHERE id = $1",
                a,
                before,
                after,
            )
        _fetch(
            url,
            "UPDATE profile_changes SET reverted_by_change_id = gen_random_uuid()"
            " WHERE agent_profile_id = $1 AND summary = $2",
            a,
            _migration().SUMMARY,
        )
        command.downgrade(config, "0051")
        assert _effort(url, a) == "high"  # changed away and back by a person
        assert _soul(url, a) == _migration().SOUL  # the fill was reverted and done again
        assert not _fetch(
            url,
            "SELECT 1 FROM profile_changes WHERE summary IN ($1, $2)",
            _migration().SUMMARY,
            _migration().EFFORT_SUMMARY,
        )
