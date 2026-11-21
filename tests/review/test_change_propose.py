"""Proposals of a change of the profile from a chat run (M6.1, docs/architecture.md, Section 9.4.2).

The agent proposes a change of the soul, the settings, the toolsets, or the policy rules of its
profile. A person with the right role approves it. The approval writes the change and a row of
the history. A person can revert a row of the history."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AgentProfile, AuditEntry, ProfileChange, ReviewProposal, ToolPolicy
from sama.profile_changes import (
    RevertError,
    StaleChangeError,
    change_diff,
    check_rules,
    list_changes,
    revert_change,
    session_snapshot,
)
from sama.profiles import NotAdminError, ProfileError
from sama.review.change import ChangeProposer, ChangeRequest
from sama.review.shown import shown_proposal
from sama.review.soul import SoulError
from tests.orchestrator.conftest import Tenant
from tests.review.support import set_profile
from tests.review.test_chat_proposals import Chat, start_chat

pytestmark = pytest.mark.usefixtures("tenant")


def proposer(chat: Chat) -> ChangeProposer:
    return ChangeProposer(chat.engine, chat.proposals)


async def profile(engine: AsyncEngine, tenant: Tenant) -> Any:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                select(AgentProfile).where(AgentProfile.id == tenant.ids["profile"])
            )
        ).one()


async def rules(engine: AsyncEngine, tenant: Tenant) -> dict[str, str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(ToolPolicy.tool, ToolPolicy.action).where(
                ToolPolicy.org_id == tenant.org,
                ToolPolicy.agent_profile_id == tenant.ids["profile"],
                ToolPolicy.user_id.is_(None),
            )
        )
        return {str(tool): str(action) for tool, action in rows}


async def history(engine: AsyncEngine, tenant: Tenant) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        return await list_changes(connection, tenant.org, tenant.ids["profile"])


async def approve(chat: Chat) -> ReviewProposal:
    assert await chat.decide("owner") == "approved"
    await chat.applier.apply_decided()
    (proposal,) = await chat.rows(ReviewProposal)
    return proposal  # type: ignore[no-any-return]


async def revert(engine: AsyncEngine, tenant: Tenant, change_id: str, user: str = "owner") -> Any:
    import uuid

    async with engine.begin() as connection:
        return await revert_change(
            connection,
            org_id=tenant.org,
            profile_id=tenant.ids["profile"],
            change_id=uuid.UUID(change_id),
            actor_user_id=tenant.ids[user],
        )


# -- toolsets ----------------------------------------------------------------------------------


async def test_a_toolsets_change_is_proposed_approved_and_reverted(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    text = await proposer(chat).propose(
        chat.claims,
        ChangeRequest(
            target="toolsets", summary="add web", toolsets=["core", "memory", "skills", "web"]
        ),
    )
    assert "Proposed a change of the toolsets" in text
    assert "next session" in text
    assert (await profile(engine, tenant)).toolsets == ["core", "memory", "skills", "self"]
    (pending,) = await chat.rows(ReviewProposal)
    assert (pending.kind, pending.scope) == ("toolsets", "org")
    assert pending.payload["before"] == {"toolsets": ["core", "memory", "skills", "self"]}
    assert pending.payload["after"] == {"toolsets": ["core", "memory", "skills", "web"]}
    shown = shown_proposal(pending.kind, pending.scope, pending.payload)
    assert shown.diff is not None
    assert "+ web" in shown.diff
    assert "- self" in shown.diff

    proposal = await approve(chat)
    assert proposal.status == "applied"
    assert (await profile(engine, tenant)).toolsets == ["core", "memory", "skills", "web"]
    (change,) = await history(engine, tenant)
    assert (change["kind"], change["source"]) == ("toolsets", "proposal")
    assert change["proposal_id"] == str(proposal.id)
    assert change["actor_user_id"] == str(tenant.ids["owner"])
    assert change["summary"] == "add web"
    assert "+ web" in change["diff"]

    reverted = await revert(engine, tenant, change["id"])
    assert (await profile(engine, tenant)).toolsets == ["core", "memory", "skills", "self"]
    back, original = await history(engine, tenant)
    assert back["id"] == str(reverted)
    assert (back["source"], back["reverts_change_id"]) == ("revert", change["id"])
    assert original["reverted_by_change_id"] == str(reverted)
    with pytest.raises(RevertError, match="already reverted"):
        await revert(engine, tenant, change["id"])
    async with engine.connect() as connection:
        details = (
            await connection.scalars(
                select(AuditEntry.details)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.action == "profile.updated")
                .order_by(AuditEntry.created_at)
            )
        ).all()
    assert [d["fields"] for d in details] == [["toolsets"], ["toolsets"]]
    assert details[1]["reverts_change_id"] == change["id"]


async def test_a_revert_fails_when_the_profile_changed_after_it(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    await proposer(chat).propose(
        chat.claims, ChangeRequest(target="profile", summary="rename", name="Helper")
    )
    await approve(chat)
    (change,) = await history(engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(name="Someone else")
        )
    with pytest.raises(StaleChangeError, match="changed after"):
        await revert(engine, tenant, change["id"])
    assert (await profile(engine, tenant)).name == "Someone else"


async def test_only_an_org_admin_reverts(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    await proposer(chat).propose(
        chat.claims, ChangeRequest(target="profile", summary="rename", name="Helper")
    )
    await approve(chat)
    (change,) = await history(engine, tenant)
    with pytest.raises(NotAdminError):
        await revert(engine, tenant, change["id"], user="admin")
    await revert(engine, tenant, change["id"], user="lead")
    assert (await profile(engine, tenant)).name != "Helper"


async def test_an_unknown_change_is_not_found(engine: AsyncEngine, tenant: Tenant) -> None:
    import uuid

    await start_chat(engine, tenant)
    with pytest.raises(LookupError):
        await revert(engine, tenant, str(uuid.uuid4()))


# -- settings ----------------------------------------------------------------------------------


async def test_a_settings_change_fails_when_the_profile_changed(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    await proposer(chat).propose(
        chat.claims, ChangeRequest(target="profile", summary="rename", name="Helper")
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(AgentProfile)
            .where(AgentProfile.id == tenant.ids["profile"])
            .values(name="Someone else")
        )
    proposal = await approve(chat)
    assert proposal.status == "failed"
    assert "changed after" in (proposal.error or "")
    assert (await profile(engine, tenant)).name == "Someone else"
    assert await history(engine, tenant) == []


async def test_a_settings_change_of_a_workspace_profile_needs_a_workspace_admin(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    from sama.gate.service import ApprovalError

    chat = await start_chat(engine, tenant)
    await set_profile(engine, tenant, workspace_id=tenant.ids["ws"])
    text = await proposer(chat).propose(
        chat.claims, ChangeRequest(target="profile", summary="rename", name="Helper")
    )
    assert "A workspace admin or the org owner must approve it first" in text
    with pytest.raises(ApprovalError, match="workspace admin or the org owner"):
        await chat.decide("mia")
    assert await chat.decide("admin") == "approved"
    await chat.applier.apply_decided()
    assert (await profile(engine, tenant)).name == "Helper"
    (change,) = await history(engine, tenant)
    assert change["actor_user_id"] == str(tenant.ids["admin"])


# -- policy ------------------------------------------------------------------------------------


async def test_a_policy_change_sets_and_removes_rules(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(
                org_id=tenant.org,
                agent_profile_id=tenant.ids["profile"],
                user_id=None,
                tool="web_fetch",
                action="allow",
            )
        )
    text = await proposer(chat).propose(
        chat.claims,
        ChangeRequest(
            target="policy",
            summary="ask before web",
            rules=[("web_*", "ask"), ("web_fetch", "none"), ("todo", "none")],
        ),
    )
    assert "policy rules" in text
    (pending,) = await chat.rows(ReviewProposal)
    assert pending.payload["before"] == {"rules": {"web_*": None, "web_fetch": "allow"}}
    assert pending.payload["after"] == {"rules": {"web_*": "ask", "web_fetch": None}}
    shown = shown_proposal(pending.kind, pending.scope, pending.payload)
    assert shown.diff is not None
    assert "web_*: no rule -> ask" in shown.diff
    assert "web_fetch: allow -> no rule" in shown.diff
    await approve(chat)
    assert await rules(engine, tenant) == {"web_*": "ask"}
    (change,) = await history(engine, tenant)
    await revert(engine, tenant, change["id"])
    assert await rules(engine, tenant) == {"web_fetch": "allow"}


async def test_the_proposal_is_checked(engine: AsyncEngine, tenant: Tenant) -> None:
    chat = await start_chat(engine, tenant)
    changes = proposer(chat)
    cases: list[tuple[ChangeRequest, str]] = [
        (ChangeRequest(target="soul", summary="x"), "full new soul"),
        (ChangeRequest(target="soul", summary="x", content="a", name="b"), "only content"),
        (ChangeRequest(target="profile", summary="x", content="a"), "only for the target soul"),
        (ChangeRequest(target="profile", summary="x", toolsets=["web"]), "not toolsets"),
        (ChangeRequest(target="toolsets", summary="x"), "at least one"),
        (ChangeRequest(target="profile", summary=" ", name="b"), "summary"),
        (ChangeRequest(target="toolsets", summary="x", toolsets=["nope"]), "nope"),
        (ChangeRequest(target="policy", summary="x", rules=[("a b", "ask")]), "not valid"),
        (ChangeRequest(target="policy", summary="x", rules=[("a", "ask"), ("a", "deny")]), "two"),
        (ChangeRequest(target="policy", summary="x", rules=[("a*b", "ask")]), "not valid"),
    ]
    for request, message in cases:
        with pytest.raises(SoulError, match=message):
            await changes.propose(chat.claims, request)
    secret = "change-secret-" + "abcdefghij" * 4
    from sama.vault.redact import default_redactor

    default_redactor().add(secret)
    try:
        with pytest.raises(SoulError, match="secret"):
            await changes.propose(
                chat.claims, ChangeRequest(target="profile", summary="x", name=secret)
            )
    finally:
        default_redactor().forget(secret)
    current = await profile(engine, tenant)
    same = await changes.propose(
        chat.claims, ChangeRequest(target="profile", summary="same", name=current.name)
    )
    assert "Nothing to propose" in same
    assert await chat.rows(ReviewProposal) == []


async def test_a_soul_change_has_a_diff_and_a_history_row(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    chat = await start_chat(engine, tenant)
    await proposer(chat).propose(
        chat.claims, ChangeRequest(target="soul", summary="be brief", content="You are brief.")
    )
    (pending,) = await chat.rows(ReviewProposal)
    assert pending.payload["before_soul"] == "You are a test agent."
    shown = shown_proposal(pending.kind, pending.scope, pending.payload)
    assert shown.diff is not None
    assert "-You are a test agent." in shown.diff
    assert "+You are brief." in shown.diff
    await approve(chat)
    (change,) = await history(engine, tenant)
    assert (change["kind"], change["source"]) == ("soul", "proposal")
    assert change["before"] == {"soul": "You are a test agent."}
    assert change["after"] == {"soul": "You are brief."}
    await revert(engine, tenant, change["id"])
    assert await chat.soul() == "You are a test agent."


# -- the next session --------------------------------------------------------------------------


async def test_the_gate_uses_the_snapshot_of_the_session(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    """A new toolset or a looser profile rule waits for the next session. A removal applies at
    once (Section 11.4)."""
    from sama.db.models import Session

    chat = await start_chat(engine, tenant)
    full = ["core", "memory", "skills", "self"]
    await set_profile(engine, tenant, group_toolsets=full)  # s1 is a shared session.
    assert "change_propose" in await chat.gate.visible_tools(chat.claims)

    async def visible(toolsets: list[str], rules: list[dict[str, str]] | None = None) -> Any:
        value = {"toolsets": toolsets, "group_toolsets": toolsets, "rules": rules or []}
        async with engine.begin() as connection:
            await connection.execute(
                update(Session).where(Session.id == tenant.ids["s1"]).values(profile_snapshot=value)
            )
        return await chat.gate.visible_tools(chat.claims)

    assert "change_propose" not in await visible(full[:3]), "self is new after the snapshot"
    tools = await visible(full, [{"tool": "memory", "action": "deny"}])
    assert "memory" not in tools, "the frozen deny rule holds until the next session"
    assert "change_propose" in tools
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(
                org_id=tenant.org,
                agent_profile_id=tenant.ids["profile"],
                user_id=None,
                tool="skill_view",
                action="deny",
            )
        )
    assert "skill_view" not in await visible(full), "a live deny rule applies at once"
    await set_profile(engine, tenant, toolsets=["core", "memory"], group_toolsets=["core"])
    assert "change_propose" not in await visible(full), "a removal applies at once"


# -- pure helpers ------------------------------------------------------------------------------


def test_change_diff_formats_each_kind() -> None:
    assert change_diff("profile", {"name": "A"}, {"name": "B"}) == "name: A -> B"
    assert change_diff("toolsets", {"group_toolsets": []}, {"group_toolsets": ["web"]}) == (
        "group_toolsets:\n+ web"
    )
    text = change_diff("profile", {"subagents": []}, {"subagents": [{"name": "r", "model": "m"}]})
    assert text.startswith("subagents: (none) -> r")
    long = change_diff("soul", {"soul": ""}, {"soul": "x\n" * 2000})
    assert len(long) <= 1500
    assert long.endswith("…")
    assert len(change_diff("soul", {"soul": ""}, {"soul": "x\n" * 2000}, cut=False)) > 1500


def test_check_rules() -> None:
    assert check_rules({"web_*": "ask", " todo ": None}) == {"web_*": "ask", "todo": None}
    with pytest.raises(ProfileError, match="not known"):
        check_rules({"a": "maybe"})
    with pytest.raises(ProfileError, match="at most"):
        check_rules({f"t{i}": "ask" for i in range(21)})
    with pytest.raises(ProfileError, match="1 to"):
        check_rules({"": "ask"})


async def test_the_session_snapshot_has_the_toolsets_and_the_rules(
    engine: AsyncEngine, tenant: Tenant
) -> None:
    await start_chat(engine, tenant)
    async with engine.begin() as connection:
        await connection.execute(
            insert(ToolPolicy).values(
                org_id=tenant.org,
                agent_profile_id=tenant.ids["profile"],
                user_id=None,
                tool="web_fetch",
                action="deny",
            )
        )
        snapshot = await session_snapshot(connection, tenant.org, tenant.ids["profile"])
    assert snapshot == {
        "toolsets": ["core", "memory", "skills", "self"],
        "group_toolsets": ["core", "todo", "web", "clarify"],
        "rules": [{"tool": "web_fetch", "action": "deny"}],
    }
    async with engine.connect() as connection:
        count = len((await connection.scalars(select(ProfileChange.id))).all())
    assert count == 0
