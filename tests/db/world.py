"""Seed data for the RLS tests: two orgs and the rows that each user may or may not read.

Org A has the workspaces ``a1`` and ``a2``. Org B has ``b1``.

- ``alice``: org A member and member of ``a1``. Owns the private session ``a1-alice``.
- ``carol``: org A admin. Sees every workspace of org A and the audit log.
- ``dave``: org A member with no workspace.
- ``bob``: org B owner and member of ``b1``. Owns the private session ``b1-bob``.
- ``stranger``: a Supabase Auth user with no sama user row.

Each org has an org-wide tool policy rule. Alice and dave each have a personal rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, insert, text
from sqlalchemy.pool import NullPool

from sama.db import create_engine
from sama.db.models import (
    EMBEDDING_DIMENSIONS,
    AgentProfile,
    Approval,
    Attachment,
    AuditEntry,
    ChannelGroup,
    ChannelIdentity,
    ContextPin,
    Integration,
    Membership,
    Memory,
    MemoryRevision,
    Message,
    MessageEmbedding,
    ModelCall,
    Org,
    PairingCode,
    ReviewProposal,
    Run,
    RunEvent,
    RuntimeSessionItem,
    Session,
    SessionBinding,
    SessionCompaction,
    SessionTodo,
    StoredContextFile,
    ToolCall,
    ToolPolicy,
    User,
    VaultSecret,
    Workspace,
)

AUTH_USERS = ("alice", "carol", "dave", "bob", "stranger")


@dataclass
class World:
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    auth: dict[str, uuid.UUID] = field(default_factory=dict)

    def new(self, label: str) -> uuid.UUID:
        value = uuid.uuid4()
        self.ids[label] = value
        return value

    def label(self, value: uuid.UUID) -> str:
        return next(label for label, known in self.ids.items() if known == value)


def plan(
    world: World, auth: dict[str, uuid.UUID] | None = None
) -> list[tuple[Any, dict[str, Any]]]:
    """The rows to insert, in foreign key order. ``auth`` sets real Supabase Auth user ids."""
    ids = world.ids
    world.auth = {name: uuid.uuid4() for name in AUTH_USERS} | (auth or {})
    rows: list[tuple[Any, dict[str, Any]]] = []

    def add(model: Any, label: str | None = None, /, **values: Any) -> None:
        if label is not None and "id" not in values:
            values["id"] = world.new(label)
        rows.append((model, values))

    add(Org, "org-a", name="Org A", slug=f"a-{uuid.uuid4().hex[:8]}")
    add(Org, "org-b", name="Org B", slug=f"b-{uuid.uuid4().hex[:8]}")
    org_of = {"alice": "org-a", "carol": "org-a", "dave": "org-a", "bob": "org-b"}
    for name, org in org_of.items():
        add(
            User,
            name,
            org_id=ids[org],
            auth_user_id=world.auth[name],
            email=f"{name}@example.test",
            display_name=name,
        )
    for slug, org in (("a1", "org-a"), ("a2", "org-a"), ("b1", "org-b")):
        add(Workspace, slug, org_id=ids[org], name=slug, slug=slug)
    memberships: tuple[tuple[str, str | None, str], ...] = (
        ("alice", None, "member"),
        ("alice", "a1", "member"),
        ("carol", None, "admin"),
        ("dave", None, "member"),
        ("bob", None, "owner"),
        ("bob", "b1", "member"),
    )
    for user, workspace, role in memberships:
        org = org_of[user]
        add(
            Membership,
            org_id=ids[org],
            user_id=ids[user],
            workspace_id=ids[workspace] if workspace else None,
            role=role,
        )
    profiles: tuple[tuple[str, str, str | None], ...] = (
        ("profile-a", "org-a", None),
        ("profile-a2", "org-a", "a2"),
        ("profile-b", "org-b", None),
    )
    for profile, org, workspace in profiles:
        add(
            AgentProfile,
            profile,
            org_id=ids[org],
            workspace_id=ids[workspace] if workspace else None,
            name=profile,
            model="claude-sonnet-5-5",
        )
    sessions = (
        ("a1-shared", "org-a", "a1", "profile-a", "shared", None),
        ("a1-alice", "org-a", "a1", "profile-a", "private", "alice"),
        ("a2-shared", "org-a", "a2", "profile-a2", "shared", None),
        ("b1-shared", "org-b", "b1", "profile-b", "shared", None),
        ("b1-bob", "org-b", "b1", "profile-b", "private", "bob"),
    )
    for session, org, workspace, profile, audience, owner in sessions:
        org_id = ids[org]
        add(
            Session,
            session,
            org_id=org_id,
            workspace_id=ids[workspace],
            agent_profile_id=ids[profile],
            audience=audience,
            owner_user_id=ids[owner] if owner else None,
            title=session,
        )
        run_id = world.new(f"run:{session}")
        add(
            Run,
            id=run_id,
            org_id=org_id,
            session_id=ids[session],
            agent_profile_id=ids[profile],
            runtime="fake",
        )
        add(
            Message,
            f"msg:{session}",
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            role="user",
            content={"text": f"secret of {session}"},
        )
        add(
            Attachment,
            org_id=org_id,
            session_id=ids[session],
            message_id=ids[f"msg:{session}"],
            name="secret.png",
            mime_type="image/png",
            size_bytes=3,
            sha256="0" * 64,
            data=b"png",
        )
        add(
            MessageEmbedding,
            org_id=org_id,
            session_id=ids[session],
            message_id=ids[f"msg:{session}"],
            embedding=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            model="test-embedding",
        )
        add(
            RunEvent,
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            seq=0,
            type="text_delta",
        )
        add(
            ToolCall,
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            call_id="call-1",
            tool_name="web_search",
        )
        add(
            Approval,
            f"approval:{session}",
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            call_id="call-1",
            tool_name="Bash",
            arguments={"command": "make test"},
            arguments_digest="0" * 64,
            expires_at=text("now() + interval '1 day'"),
        )
        add(
            ReviewProposal,
            f"proposal:{session}",
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            approval_id=ids[f"approval:{session}"],
            kind="memory",
            scope="workspace",
            payload={"target": "workspace", "operations": []},
        )
        add(
            SessionBinding,
            org_id=org_id,
            workspace_id=ids[workspace],
            session_id=ids[session],
            channel="web",
            external_key=f"web:{ids[session]}",
        )
        add(
            ModelCall,
            call_id=uuid.uuid4(),
            org_id=org_id,
            workspace_id=ids[workspace],
            session_id=ids[session],
            run_id=run_id,
            user_id=ids[owner] if owner else None,
            provider="anthropic",
            model="claude-sonnet-5-5",
            endpoint="messages",
            status_code=200,
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.00007"),
            duration_ms=12,
        )
        add(
            SessionTodo,
            org_id=org_id,
            workspace_id=ids[workspace],
            session_id=ids[session],
            items=[{"id": "1", "content": f"todo of {session}", "status": "pending"}],
            run_id=run_id,
        )
        add(
            SessionCompaction,
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            trigger="auto",
        )
        add(
            ContextPin,
            org_id=org_id,
            session_id=ids[session],
            text=f"pin of {session}",
        )
        add(
            RuntimeSessionItem,
            org_id=org_id,
            session_id=ids[session],
            conversation="c-world",
            seq=0,
            item={"role": "user", "content": f"item of {session}"},
        )
    policies: tuple[tuple[str, str, str | None], ...] = (
        ("rule:org-a", "org-a", None),
        ("rule:org-b", "org-b", None),
        ("rule:alice", "org-a", "alice"),
        ("rule:dave", "org-a", "dave"),
    )
    for label, org, owner in policies:
        add(
            ToolPolicy,
            label,
            org_id=ids[org],
            user_id=ids[owner] if owner else None,
            tool="Bash",
            action="allow" if owner else "ask",
        )
    # file:profile-a2 has no workspace, but its profile is in a2.
    files: tuple[tuple[str, str, str | None, str | None], ...] = (
        ("file:org-a", "org-a", None, None),
        ("file:a2", "org-a", "a2", None),
        ("file:profile-a2", "org-a", None, "profile-a2"),
        ("file:org-b", "org-b", None, None),
    )
    for label, org, workspace, owner_profile in files:
        add(
            StoredContextFile,
            label,
            org_id=ids[org],
            workspace_id=ids[workspace] if workspace else None,
            agent_profile_id=ids[owner_profile] if owner_profile else None,
            path="AGENTS.md",
            content=f"rules of {label}",
        )
    # One Telegram bot for each org: every member sees it.
    for user, org in (("alice", "org-a"), ("bob", "org-b")):
        add(
            Integration,
            f"int:{org}",
            org_id=ids[org],
            kind="telegram",
            status="active",
            external_id=str(uuid.uuid4().int % 10**10),
            name=f"{org}_bot",
            created_by_user_id=ids[user],
        )
    # Telegram links: each user reads only their own; a group shows to the workspace.
    for user, org, workspace, profile in (
        ("alice", "org-a", "a1", "profile-a"),
        ("bob", "org-b", "b1", "profile-b"),
    ):
        links = {
            "org_id": ids[org],
            "channel": "telegram",
            "integration_id": ids[f"int:{org}"],
            "workspace_id": ids[workspace],
            "agent_profile_id": ids[profile],
        }
        add(
            ChannelIdentity,
            f"tg:{user}",
            user_id=ids[user],
            external_id=str(uuid.uuid4().int % 10**12),
            **links,
        )
        add(
            ChannelGroup,
            f"group:{workspace}",
            created_by_user_id=ids[user],
            external_id=f"-{uuid.uuid4().int % 10**12}",
            **links,
        )
        add(
            PairingCode,
            f"code:{user}",
            user_id=ids[user],
            kind="user",
            code_hash=uuid.uuid4().hex * 2,
            expires_at=text("now() + interval '10 minutes'"),
            **links,
        )
    # Notes: the user notes belong to one user, the workspace notes to a workspace, and the
    # agent notes to a profile in a workspace.
    notes: tuple[tuple[str, str, str, dict[str, str]], ...] = (
        ("mem:alice", "org-a", "user", {"user": "alice"}),
        ("mem:dave", "org-a", "user", {"user": "dave"}),
        ("mem:a1", "org-a", "workspace", {"workspace": "a1"}),
        ("mem:a2", "org-a", "workspace", {"workspace": "a2"}),
        ("mem:a1-agent", "org-a", "agent", {"workspace": "a1", "profile": "profile-a"}),
        ("mem:bob", "org-b", "user", {"user": "bob"}),
        ("mem:b1", "org-b", "workspace", {"workspace": "b1"}),
    )
    for label, org, scope, keys in notes:
        add(
            Memory,
            label,
            org_id=ids[org],
            scope=scope,
            workspace_id=ids[keys["workspace"]] if "workspace" in keys else None,
            agent_profile_id=ids[keys["profile"]] if "profile" in keys else None,
            user_id=ids[keys["user"]] if "user" in keys else None,
            entries=[f"note of {label}"],
            revision=1,
        )
        add(
            MemoryRevision,
            f"rev:{label}",
            org_id=ids[org],
            memory_id=ids[label],
            seq=1,
            action="add",
            entries=[f"note of {label}"],
            author_kind="agent",
        )
    for org in ("org-a", "org-b"):
        add(AuditEntry, org_id=ids[org], actor_kind="system", action=f"seed {org}")
        add(
            VaultSecret,
            org_id=ids[org],
            name="brave_api_key",
            key_id="seed",
            wrapped_key=b"wrapped",
            ciphertext=b"sealed",
        )
    return rows


async def seed(url: str, auth: dict[str, uuid.UUID] | None = None) -> World:
    world = World()
    engine = create_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for model, values in plan(world, auth):
                await connection.execute(insert(model).values(**values))
    finally:
        await engine.dispose()
    return world


async def unseed(url: str, world: World) -> None:
    """Deletes the two orgs of ``world`` and all their rows."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
            await connection.execute(
                delete(Org).where(Org.id.in_([world.ids["org-a"], world.ids["org-b"]]))
            )
    finally:
        await engine.dispose()
