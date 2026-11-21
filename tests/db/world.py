"""Seed data for the RLS tests: two orgs and the rows that each user may or may not read.

Org A has the workspaces ``a1`` and ``a2``. Org B has ``b1``.

- ``alice``: org A member and member of ``a1``. Owns the private session ``a1-alice`` and the
  job ``job:alice``.
- ``carol``: org A admin. Sees every workspace of org A and the audit log.
- ``dave``: org A member with no workspace.
- ``bob``: org B owner and member of ``b1``. Owns the private session ``b1-bob`` and the job
  ``job:bob``.
- ``stranger``: a Supabase Auth user with no sama user row.

Each org has an org-wide tool policy rule. Alice and dave each have a personal rule. Each org
has one GitHub installation, one allowlisted repository, and one push grant.
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
    Connection,
    ContextPin,
    Delivery,
    GeneratedMedia,
    GitHubInstallation,
    GitHubPushGrant,
    GitHubRepo,
    Integration,
    Job,
    JobFire,
    Membership,
    Memory,
    MemoryRevision,
    Message,
    MessageEmbedding,
    ModelCall,
    Org,
    OrgInvite,
    PairingCode,
    ReviewProposal,
    Run,
    RunEvent,
    RunSteer,
    RuntimeSessionItem,
    Session,
    SessionBinding,
    SessionCompaction,
    SessionHandoff,
    SessionTodo,
    StoredContextFile,
    Task,
    TaskComment,
    ToolCall,
    ToolPolicy,
    Trigger,
    TriggerEvent,
    User,
    VaultSecret,
    WorkerBot,
    Workflow,
    WorkflowInstance,
    WorkflowReceipt,
    WorkflowStep,
    WorkflowVersion,
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
    # An open invite in each org. Only the admins and owners of the org read it.
    for org, inviter, workspace in (("org-a", "carol", "a1"), ("org-b", "bob", None)):
        add(
            OrgInvite,
            f"invite:{org}",
            org_id=ids[org],
            email=f"new-{org}@example.test",
            role="member",
            workspace_id=ids[workspace] if workspace else None,
            token_hash=uuid.uuid4().bytes * 2,
            invited_by_user_id=ids[inviter],
            expires_at=text("now() + interval '7 days'"),
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
            GeneratedMedia,
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            kind="image",
            mime_type="image/png",
            size_bytes=3,
            sha256="0" * 64,
            caption=f"image of {session}",
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
        # One task card with the audience of the session, and one comment on it.
        add(
            Task,
            f"task:{session}",
            org_id=org_id,
            workspace_id=ids[workspace],
            title=f"task of {session}",
            audience=audience,
            owner_user_id=ids[owner] if owner else None,
        )
        add(
            TaskComment,
            f"comment:{session}",
            org_id=org_id,
            workspace_id=ids[workspace],
            task_id=ids[f"task:{session}"],
            author_kind="user",
            body=f"comment of {session}",
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
        add(
            RunSteer,
            org_id=org_id,
            session_id=ids[session],
            run_id=run_id,
            user_id=ids[owner] if owner else None,
            text=f"steer of {session}",
        )
        add(
            SessionHandoff,
            org_id=org_id,
            workspace_id=ids[workspace],
            session_id=ids[session],
            user_id=ids[owner] if owner else None,
            from_channel="telegram",
            from_key=f"telegram:{ids[session]}",
            to_channel="web",
        )
    # One job for each owner, with a fire and a delivery. A job is private to its owner.
    for user, org, workspace, profile in (
        ("alice", "org-a", "a1", "profile-a"),
        ("bob", "org-b", "b1", "profile-b"),
    ):
        add(
            Job,
            f"job:{user}",
            org_id=ids[org],
            workspace_id=ids[workspace],
            owner_user_id=ids[user],
            agent_profile_id=ids[profile],
            name=f"job of {user}",
            schedule={"kind": "interval", "expr": "3600"},
            prompt=f"secret prompt of {user}",
        )
        add(
            JobFire,
            f"fire:{user}",
            org_id=ids[org],
            job_id=ids[f"job:{user}"],
            job_version=1,
            scheduled_for=text("now()"),
        )
        add(
            Delivery,
            f"delivery:{user}",
            org_id=ids[org],
            job_id=ids[f"job:{user}"],
            fire_id=ids[f"fire:{user}"],
            kind="result",
            target="telegram:1",
            text=f"secret result of {user}",
        )
    # One workflow for each owner, with a version, an instance, a step, and a receipt. A
    # workflow is private to its owner (M4.5).
    for user, org, workspace, profile in (
        ("alice", "org-a", "a1", "profile-a"),
        ("bob", "org-b", "b1", "profile-b"),
    ):
        add(
            Workflow,
            f"workflow:{user}",
            org_id=ids[org],
            workspace_id=ids[workspace],
            owner_user_id=ids[user],
            agent_profile_id=ids[profile],
            name=f"flow-of-{user}",
        )
        add(
            WorkflowVersion,
            f"workflow-version:{user}",
            org_id=ids[org],
            workflow_id=ids[f"workflow:{user}"],
            version=1,
            source=f"secret source of {user}",
            definition={"name": f"flow-of-{user}", "steps": []},
            hash="0" * 64,
        )
        add(
            WorkflowInstance,
            f"workflow-instance:{user}",
            org_id=ids[org],
            workflow_id=ids[f"workflow:{user}"],
            version=1,
            owner_user_id=ids[user],
            workspace_id=ids[workspace],
            agent_profile_id=ids[profile],
        )
        add(
            WorkflowStep,
            f"workflow-step:{user}",
            org_id=ids[org],
            instance_id=ids[f"workflow-instance:{user}"],
            step_id="post",
            kind="tool",
        )
        add(
            WorkflowReceipt,
            f"workflow-receipt:{user}",
            org_id=ids[org],
            instance_id=ids[f"workflow-instance:{user}"],
            step_row_id=ids[f"workflow-step:{user}"],
            step_id="post",
            operation_id=f"op-of-{user}",
            attempt=1,
            tool="notes_write",
            request_digest="0" * 64,
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
    # Connections: a workspace connection shows to the workspace, a personal connection only to
    # its owner (Section 10.3).
    connections: tuple[tuple[str, str, str, str | None, str], ...] = (
        ("conn:a1", "org-a", "a1", None, "alice"),
        ("conn:a1-alice", "org-a", "a1", "alice", "alice"),
        ("conn:a2", "org-a", "a2", None, "carol"),
        ("conn:b1", "org-b", "b1", None, "bob"),
        ("conn:b1-bob", "org-b", "b1", "bob", "bob"),
    )
    for label, org, workspace, owner, creator in connections:
        add(
            Connection,
            label,
            org_id=ids[org],
            workspace_id=ids[workspace],
            scope="personal" if owner else "workspace",
            owner_user_id=ids[owner] if owner else None,
            connector="gmail",
            kind="composio",
            created_by_user_id=ids[creator],
        )
    # Event triggers show to the workspace. A trigger event shows with its session.
    triggers: tuple[tuple[str, str, str, str, str], ...] = (
        ("trig:a1", "org-a", "a1", "alice", "profile-a"),
        ("trig:a2", "org-a", "a2", "carol", "profile-a2"),
        ("trig:b1", "org-b", "b1", "bob", "profile-b"),
    )
    for label, org, workspace, owner, profile in triggers:
        add(
            Trigger,
            label,
            org_id=ids[org],
            workspace_id=ids[workspace],
            owner_user_id=ids[owner],
            agent_profile_id=ids[profile],
            kind="webhook",
            name=label,
            prompt=f"prompt of {label}",
            created_by_user_id=ids[owner],
        )
    for session, org, workspace, _, _, _ in sessions:
        add(
            TriggerEvent,
            org_id=ids[org],
            trigger_id=ids[f"trig:{workspace}"],
            delivery_id=f"msg_{session}",
            session_id=ids[session],
            run_id=ids[f"run:{session}"],
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
            WorkerBot,
            f"bot:{workspace}",
            org_id=ids[org],
            workspace_id=ids[workspace],
            agent_profile_id=ids[profile],
            integration_id=ids[f"int:{org}"],
            external_id=f"C{uuid.uuid4().hex[:10].upper()}",
            name=f"bot {workspace}",
            daily_budget_usd=5,
            created_by_user_id=ids[user],
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
    # One GitHub installation, one repository, and one push grant for each org.
    for user, org, session in (
        ("alice", "org-a", "a1-shared"),
        ("bob", "org-b", "b1-shared"),
    ):
        add(
            GitHubInstallation,
            f"gh-inst:{org}",
            org_id=ids[org],
            installation_id=uuid.uuid4().int % 10**9,
            account_login=f"{org}-account",
            account_type="Organization",
            created_by_user_id=ids[user],
        )
        add(
            GitHubRepo,
            f"gh-repo:{org}",
            org_id=ids[org],
            installation_ref=ids[f"gh-inst:{org}"],
            owner="Acme",
            name=f"{org}-app",
            full_name=f"acme/{org}-app",
            created_by_user_id=ids[user],
        )
        add(
            GitHubPushGrant,
            f"gh-grant:{org}",
            org_id=ids[org],
            repo_id=ids[f"gh-repo:{org}"],
            ref="refs/heads/sama/seed",
            commit_sha="a" * 40,
            session_id=ids[session],
            run_id=ids[f"run:{session}"],
            expires_at=text("now() + interval '1 hour'"),
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
