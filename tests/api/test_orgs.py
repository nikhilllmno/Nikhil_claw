"""Tests of sign-up, invites, and roles over HTTP (docs/architecture.md, Section 17.2)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.api import ApiSettings, JwtVerifier, SamaApi
from sama.db.models import AgentProfile, AuditEntry, Membership, Org, OrgInvite, User
from sama.orgs import OrgService, SignupPolicy
from sama.orgs import service as org_service
from sama.orgs.plans import FREE, Plan
from sama.tenancy.regions import Regions, set_home_regions
from tests.api.conftest import ISSUER, SECRET, bearer


class FakeEmails:
    """Verified emails by Supabase Auth user. A user without an entry has none."""

    def __init__(self) -> None:
        self.emails: dict[uuid.UUID, str] = {}

    async def verified_email(self, auth_user_id: uuid.UUID) -> str | None:
        return self.emails.get(auth_user_id)


class World:
    def __init__(self, engine: AsyncEngine, emails: FakeEmails) -> None:
        self.engine = engine
        self.emails = emails
        self.users: list[uuid.UUID] = []

    def person(self, email: str | None) -> uuid.UUID:
        auth_user_id = uuid.uuid4()
        self.users.append(auth_user_id)
        if email is not None:
            self.emails.emails[auth_user_id] = email
        return auth_user_id


@pytest.fixture
def emails() -> FakeEmails:
    return FakeEmails()


@pytest.fixture
def policy() -> SignupPolicy:
    return SignupPolicy(mode="open", blocked_domains=("blocked.test",))


@pytest.fixture
async def world(engine: AsyncEngine, emails: FakeEmails) -> AsyncIterator[World]:
    world = World(engine, emails)
    yield world
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        org_ids = (
            await connection.scalars(select(User.org_id).where(User.auth_user_id.in_(world.users)))
        ).all()
        await connection.execute(
            delete(Org).where(Org.id.in_(org_ids) | Org.created_by_auth_user_id.in_(world.users))
        )


@pytest.fixture
async def web(
    engine: AsyncEngine, emails: FakeEmails, policy: SignupPolicy
) -> AsyncIterator[httpx.AsyncClient]:
    api = SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        settings=ApiSettings(supabase_url="https://project.example.test"),
        orgs=OrgService(engine, emails, policy),
    )
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


async def new_org(web: httpx.AsyncClient, owner: uuid.UUID, name: str = "Acme") -> dict[str, Any]:
    response = await web.post("/orgs", json={"name": name}, headers=bearer(owner))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def invite(
    web: httpx.AsyncClient, admin: uuid.UUID, org_id: str, email: str, **extra: Any
) -> httpx.Response:
    return await web.post(
        f"/orgs/{org_id}/invites", json={"email": email, **extra}, headers=bearer(admin)
    )


async def join(
    web: httpx.AsyncClient, owner: uuid.UUID, org_id: str, world: World, email: str, **extra: Any
) -> tuple[uuid.UUID, str]:
    """Invites a new person and accepts the invite. Returns the auth id and the user id."""
    person = world.person(email)
    created = await invite(web, owner, org_id, email, **extra)
    assert created.status_code == 201, created.text
    accepted = await web.post(
        "/invites/accept", json={"token": created.json()["token"]}, headers=bearer(person)
    )
    assert accepted.status_code == 200, accepted.text
    return person, str(accepted.json()["user_id"])


async def me(web: httpx.AsyncClient, person: uuid.UUID) -> list[dict[str, Any]]:
    orgs: list[dict[str, Any]] = (await web.get("/me", headers=bearer(person))).json()["orgs"]
    return orgs


async def members(web: httpx.AsyncClient, person: uuid.UUID, org_id: str) -> dict[str, Any]:
    response = await web.get(f"/orgs/{org_id}/members", headers=bearer(person))
    assert response.status_code == 200, response.text
    return {m["email"]: m for m in response.json()}


# -- org creation ----------------------------------------------------------------------------


async def test_sign_up_is_closed_by_default(client: httpx.AsyncClient) -> None:
    assert (await client.get("/config")).json()["signup"] == "invite_only"
    response = await client.post("/orgs", json={"name": "Acme"}, headers=bearer(uuid.uuid4()))
    assert response.status_code == 403
    assert "closed" in response.json()["detail"]


async def test_a_verified_user_makes_an_org_and_owns_it(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    assert (await web.get("/config")).json()["signup"] == "open"
    ana = world.person("ana@example.test")
    org = await new_org(web, ana, "Acme Labs")
    assert (org["name"], org["slug"], org["plan"]) == ("Acme Labs", "acme-labs", "free")
    [seen] = await me(web, ana)
    assert (seen["id"], seen["role"], seen["admin"], seen["plan"]) == (
        org["id"],
        "owner",
        True,
        "free",
    )
    assert seen["display_name"] == "ana"
    assert [w["name"] for w in seen["workspaces"]] == ["Main"]
    assert [p["name"] for p in seen["agent_profiles"]] == ["sama"]
    async with engine.connect() as connection:
        limits = await connection.scalar(select(Org.limits).where(Org.id == uuid.UUID(org["id"])))
        budget = await connection.scalar(
            select(AgentProfile.budget).where(AgentProfile.org_id == uuid.UUID(org["id"]))
        )
        actions = (
            await connection.scalars(
                select(AuditEntry.action).where(AuditEntry.org_id == uuid.UUID(org["id"]))
            )
        ).all()
    assert limits == dict(FREE.limits)
    assert budget == dict(FREE.run_budget)
    assert actions == ["org.created"]
    # The same name again gets a free slug.
    again = await new_org(web, ana, "Acme Labs")
    assert again["slug"].startswith("acme-labs-")


@pytest.mark.parametrize(
    ("email", "detail"),
    [
        (None, "confirm your email"),
        ("eve@mailinator.com", "domain"),
        ("eve@blocked.test", "domain"),
        ("eve@mx.blocked.test", "domain"),
    ],
)
async def test_an_org_needs_a_verified_email_from_an_allowed_domain(
    web: httpx.AsyncClient, world: World, email: str | None, detail: str
) -> None:
    response = await web.post("/orgs", json={"name": "Eve"}, headers=bearer(world.person(email)))
    assert response.status_code == 403
    assert detail in response.json()["detail"]


async def test_a_slug_is_checked(web: httpx.AsyncClient, world: World) -> None:
    ana = world.person("ana@example.test")
    slug = f"acme-{uuid.uuid4().hex[:6]}"
    first = await web.post("/orgs", json={"name": "A", "slug": slug}, headers=bearer(ana))
    assert first.status_code == 201
    taken = await web.post("/orgs", json={"name": "A", "slug": slug}, headers=bearer(ana))
    assert taken.status_code == 409
    for bad in ("admin", "Bad Slug", "a"):
        response = await web.post("/orgs", json={"name": "A", "slug": bad}, headers=bearer(ana))
        assert response.status_code == 422, bad


@pytest.mark.parametrize(
    ("policy", "same_user", "detail"),
    [
        (SignupPolicy(mode="open", orgs_per_user_per_day=2), True, "in a day"),
        (SignupPolicy(mode="open", orgs_per_user=2, orgs_per_user_per_day=5), True, "at most 2"),
        (SignupPolicy(mode="open", orgs_per_hour=2), False, "try again later"),
    ],
)
async def test_org_creation_has_rate_limits(
    web: httpx.AsyncClient,
    engine: AsyncEngine,
    world: World,
    policy: SignupPolicy,
    same_user: bool,
    detail: str,
) -> None:
    if not same_user:
        # Other tests made orgs in the last hour: start the count of this test at zero.
        async with engine.begin() as connection:
            await connection.execute(
                update(Org)
                .where(Org.created_by_auth_user_id.is_not(None))
                .values(created_at=func.now() - text("interval '2 hours'"))
            )
    ana = world.person("ana@example.test")
    for _ in range(2):
        person = ana if same_user else world.person("someone@example.test")
        await new_org(web, person)
    person = ana if same_user else world.person("late@example.test")
    response = await web.post("/orgs", json={"name": "One more"}, headers=bearer(person))
    assert response.status_code == 429
    assert detail in response.json()["detail"]


# -- invites ---------------------------------------------------------------------------------


async def test_an_invite_adds_a_member_to_a_workspace(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    [ana_org] = await me(web, ana)
    workspace = ana_org["workspaces"][0]["id"]
    created = await invite(web, ana, org["id"], " Bob@Example.TEST ", workspace_id=workspace)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["email"], body["role"], body["workspace_id"]) == (
        "bob@example.test",
        "member",
        workspace,
    )
    token = body["token"]
    listed = (await web.get(f"/orgs/{org['id']}/invites", headers=bearer(ana))).json()
    assert [i["id"] for i in listed] == [body["id"]]
    assert "token" not in listed[0]
    async with engine.connect() as connection:
        stored = await connection.scalar(
            select(OrgInvite.token_hash).where(OrgInvite.id == uuid.UUID(body["id"]))
        )
    assert stored == org_service.hash_token(token)
    assert token.encode() not in (stored or b"")

    bob = world.person("bob@example.test")
    pending = (await web.get("/invites", headers=bearer(bob))).json()
    assert [(i["id"], i["org_name"]) for i in pending] == [(body["id"], "Acme")]
    accepted = await web.post("/invites/accept", json={"token": token}, headers=bearer(bob))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "member"
    [bob_org] = await me(web, bob)
    assert (bob_org["id"], bob_org["role"], bob_org["admin"]) == (org["id"], "member", False)
    assert [w["id"] for w in bob_org["workspaces"]] == [workspace]
    assert (await web.get("/invites", headers=bearer(bob))).json() == []
    # An invite works once.
    again = await web.post("/invites/accept", json={"token": token}, headers=bearer(bob))
    assert again.status_code == 410
    team = await members(web, bob, org["id"])
    assert {email: m["role"] for email, m in team.items()} == {
        "ana@example.test": "owner",
        "bob@example.test": "member",
    }
    assert team["bob@example.test"]["workspace_ids"] == [workspace]
    async with engine.connect() as connection:
        actions = (
            await connection.scalars(
                select(AuditEntry.action)
                .where(AuditEntry.org_id == uuid.UUID(org["id"]))
                .order_by(AuditEntry.id)
            )
        ).all()
    assert actions == ["org.created", "invite.created", "invite.accepted"]


async def test_an_invite_is_for_its_email_only(web: httpx.AsyncClient, world: World) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    created = (await invite(web, ana, org["id"], "bob@example.test")).json()
    eve = world.person("eve@example.test")
    unverified = world.person(None)
    for person, status in ((eve, 403), (unverified, 403)):
        response = await web.post(
            "/invites/accept", json={"token": created["token"]}, headers=bearer(person)
        )
        assert response.status_code == status
    # By id, an invite of another email does not exist.
    by_id = await web.post(
        "/invites/accept", json={"invite_id": created["id"]}, headers=bearer(eve)
    )
    assert by_id.status_code == 404
    unknown = await web.post("/invites/accept", json={"token": "x" * 43}, headers=bearer(eve))
    assert unknown.status_code == 404
    both = await web.post(
        "/invites/accept",
        json={"token": created["token"], "invite_id": created["id"]},
        headers=bearer(eve),
    )
    assert both.status_code == 422


async def test_a_user_accepts_by_id_from_the_welcome_page(
    web: httpx.AsyncClient, world: World
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    created = (await invite(web, ana, org["id"], "bob@example.test", role="admin")).json()
    bob = world.person("bob@example.test")
    response = await web.post(
        "/invites/accept",
        json={"invite_id": created["id"], "display_name": "Bobby"},
        headers=bearer(bob),
    )
    assert response.status_code == 200, response.text
    [bob_org] = await me(web, bob)
    assert (bob_org["role"], bob_org["admin"], bob_org["display_name"]) == ("admin", True, "Bobby")


async def test_an_ended_invite_cannot_be_used(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    bob = world.person("bob@example.test")
    expired = (await invite(web, ana, org["id"], "bob@example.test")).json()
    async with engine.begin() as connection:
        await connection.execute(
            update(OrgInvite)
            .where(OrgInvite.id == uuid.UUID(expired["id"]))
            .values(expires_at=func.now() - text("interval '1 minute'"))
        )
    assert (await web.get("/invites", headers=bearer(bob))).json() == []
    listed = (await web.get(f"/orgs/{org['id']}/invites", headers=bearer(ana))).json()
    assert [i["expired"] for i in listed] == [True]
    response = await web.post(
        "/invites/accept", json={"token": expired["token"]}, headers=bearer(bob)
    )
    assert response.status_code == 410
    # A new invite for the same email ends the old one.
    first = (await invite(web, ana, org["id"], "bob@example.test")).json()
    second = (await invite(web, ana, org["id"], "bob@example.test")).json()
    response = await web.post(
        "/invites/accept", json={"token": first["token"]}, headers=bearer(bob)
    )
    assert response.status_code == 410
    revoked = await web.delete(f"/orgs/{org['id']}/invites/{second['id']}", headers=bearer(ana))
    assert revoked.status_code == 204
    assert (
        await web.delete(f"/orgs/{org['id']}/invites/{second['id']}", headers=bearer(ana))
    ).status_code == 404
    response = await web.post(
        "/invites/accept", json={"token": second["token"]}, headers=bearer(bob)
    )
    assert response.status_code == 410
    assert (await web.get(f"/orgs/{org['id']}/invites", headers=bearer(ana))).json() == []


async def test_only_admins_invite(web: httpx.AsyncClient, world: World) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    bob, _ = await join(web, ana, org["id"], world, "bob@example.test")
    assert (await invite(web, bob, org["id"], "cy@example.test")).status_code == 403
    assert (await web.get(f"/orgs/{org['id']}/invites", headers=bearer(bob))).status_code == 403
    stranger = world.person("x@example.test")
    assert (await invite(web, stranger, org["id"], "cy@example.test")).status_code == 404
    assert (
        await web.get(f"/orgs/{org['id']}/members", headers=bearer(stranger))
    ).status_code == 404
    # A member is invited once; an owner is never made by an invite.
    assert (await invite(web, ana, org["id"], "bob@example.test")).status_code == 409
    assert (await invite(web, ana, org["id"], "cy@example.test", role="owner")).status_code == 422
    assert (await invite(web, ana, org["id"], "not-an-email")).status_code == 422
    other = await new_org(web, world.person("zed@example.test"), "Other")
    [zed_org] = [o for o in await me(web, world.users[-1]) if o["id"] == other["id"]]
    foreign = await invite(
        web, ana, org["id"], "cy@example.test", workspace_id=zed_org["workspaces"][0]["id"]
    )
    assert foreign.status_code == 404


async def test_a_removed_user_rejoins_but_a_member_cannot_join_twice(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    bob, bob_id = await join(web, ana, org["id"], world, "bob@example.test")
    # The user row of bob has another email: the invite check does not see him as a member.
    async with engine.begin() as connection:
        await connection.execute(
            update(User).where(User.id == uuid.UUID(bob_id)).values(email="old@example.test")
        )
    created = (await invite(web, ana, org["id"], "bob@example.test")).json()
    twice = await web.post("/invites/accept", json={"token": created["token"]}, headers=bearer(bob))
    assert twice.status_code == 409
    removed = await web.delete(f"/orgs/{org['id']}/members/{bob_id}", headers=bearer(ana))
    assert removed.status_code == 204
    assert await me(web, bob) == []
    rejoin = await web.post(
        "/invites/accept", json={"token": created["token"]}, headers=bearer(bob)
    )
    assert rejoin.status_code == 200
    assert rejoin.json()["user_id"] == bob_id


async def test_the_plan_caps_members_and_invites(
    web: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    # Free: 5 members, open invites included. Ana is one.
    for n in range(4):
        assert (await invite(web, ana, org["id"], f"p{n}@example.test")).status_code == 201
    capped = await invite(web, ana, org["id"], "p4@example.test")
    assert capped.status_code == 403
    assert "5 members" in capped.json()["detail"]
    # A new invite for an invited email replaces the old one, so it fits.
    assert (await invite(web, ana, org["id"], "p0@example.test")).status_code == 201

    tiny = Plan("tiny", max_members=None, invites_per_day=1)
    monkeypatch.setattr(org_service, "plan_of", lambda name: tiny)
    limited = await invite(web, ana, org["id"], "p9@example.test")
    assert limited.status_code == 429


# -- members and roles -----------------------------------------------------------------------


async def test_roles_follow_the_owner_rules(web: httpx.AsyncClient, world: World) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    org_id = org["id"]
    ana_id = org["user_id"]
    bob, bob_id = await join(web, ana, org_id, world, "bob@example.test")
    cy, cy_id = await join(web, ana, org_id, world, "cy@example.test")

    def change(person: uuid.UUID, user_id: str, **body: Any) -> Any:
        return web.patch(f"/orgs/{org_id}/members/{user_id}", json=body, headers=bearer(person))

    # A member changes nobody.
    assert (await change(cy, bob_id, role="admin")).status_code == 403
    # The owner makes bob an admin.
    promoted = await change(ana, bob_id, role="admin")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "admin"
    # An admin changes members, but not owners, and does not give the role owner.
    assert (await change(bob, cy_id, role="admin")).status_code == 200
    assert (await change(bob, cy_id, role="member")).status_code == 200
    assert (await change(bob, cy_id, role="owner")).status_code == 403
    assert (await change(bob, ana_id, role="member")).status_code == 403
    # The last owner stays.
    assert (await change(ana, ana_id, role="admin")).status_code == 409
    leave = await web.delete(f"/orgs/{org_id}/members/{ana_id}", headers=bearer(ana))
    assert leave.status_code == 409
    # With a second owner, the first can step down.
    assert (await change(ana, bob_id, role="owner")).status_code == 200
    assert (await change(ana, ana_id, role="admin")).status_code == 200
    roles = {email: m["role"] for email, m in (await members(web, cy, org_id)).items()}
    assert roles == {
        "ana@example.test": "admin",
        "bob@example.test": "owner",
        "cy@example.test": "member",
    }
    # An admin cannot remove the owner. Nobody touches a user of another org.
    assert (
        await web.delete(f"/orgs/{org_id}/members/{bob_id}", headers=bearer(ana))
    ).status_code == 403
    assert (await change(ana, str(uuid.uuid4()), role="member")).status_code == 404
    assert (await change(ana, cy_id)).status_code == 422


async def test_workspaces_and_removal(web: httpx.AsyncClient, world: World) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    org_id = org["id"]
    workspace = org["workspace_id"]
    bob, bob_id = await join(web, ana, org_id, world, "bob@example.test")
    cy, cy_id = await join(web, ana, org_id, world, "cy@example.test")
    [bob_org] = await me(web, bob)
    assert bob_org["workspaces"] == []
    added = await web.patch(
        f"/orgs/{org_id}/members/{bob_id}",
        json={"workspace_ids": [workspace]},
        headers=bearer(ana),
    )
    assert added.status_code == 200
    assert added.json()["workspace_ids"] == [workspace]
    [bob_org] = await me(web, bob)
    assert [w["id"] for w in bob_org["workspaces"]] == [workspace]
    unknown = await web.patch(
        f"/orgs/{org_id}/members/{bob_id}",
        json={"workspace_ids": [str(uuid.uuid4())]},
        headers=bearer(ana),
    )
    assert unknown.status_code == 404
    cleared = await web.patch(
        f"/orgs/{org_id}/members/{bob_id}", json={"workspace_ids": []}, headers=bearer(ana)
    )
    assert cleared.json()["workspace_ids"] == []
    # A member removes nobody else, but can leave.
    assert (
        await web.delete(f"/orgs/{org_id}/members/{bob_id}", headers=bearer(cy))
    ).status_code == 403
    assert (
        await web.delete(f"/orgs/{org_id}/members/{cy_id}", headers=bearer(cy))
    ).status_code == 204
    assert await me(web, cy) == []
    assert (
        await web.delete(f"/orgs/{org_id}/members/{bob_id}", headers=bearer(ana))
    ).status_code == 204
    assert set(await members(web, ana, org_id)) == {"ana@example.test"}
    assert (
        await web.delete(f"/orgs/{org_id}/members/{bob_id}", headers=bearer(ana))
    ).status_code == 404


async def test_memberships_of_the_org_stay_consistent(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    """Removal deletes every membership of the user in the org and keeps the user row."""
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    _, bob_id = await join(
        web, ana, org["id"], world, "bob@example.test", workspace_id=org["workspace_id"]
    )
    await web.delete(f"/orgs/{org['id']}/members/{bob_id}", headers=bearer(ana))
    async with engine.connect() as connection:
        rows = await connection.scalar(
            select(func.count()).where(Membership.user_id == uuid.UUID(bob_id))
        )
        user = await connection.scalar(select(User.id).where(User.id == uuid.UUID(bob_id)))
    assert rows == 0
    assert user == uuid.UUID(bob_id)


# -- concurrency -----------------------------------------------------------------------------


async def test_parallel_org_creation_keeps_the_rate_limit(
    web: httpx.AsyncClient, world: World
) -> None:
    ana = world.person("ana@example.test")
    answers = await asyncio.gather(
        *(web.post("/orgs", json={"name": f"Acme {n}"}, headers=bearer(ana)) for n in range(5))
    )
    codes = sorted(a.status_code for a in answers)
    assert codes == [201, 201, 201, 429, 429], [a.text for a in answers]


async def test_parallel_accept_and_revoke_give_a_clear_answer(
    web: httpx.AsyncClient, world: World
) -> None:
    """Accept locks the org before the invite, like revoke and reissue: no deadlock, no 500."""
    ana = world.person("ana@example.test")
    org_id = (await new_org(web, ana))["id"]
    for round_ in range(4):
        email = f"bob{round_}@example.test"
        bob = world.person(email)
        created = await invite(web, ana, org_id, email)
        assert created.status_code == 201, created.text
        accept, revoke, reissue = await asyncio.gather(
            web.post(
                "/invites/accept", json={"token": created.json()["token"]}, headers=bearer(bob)
            ),
            web.delete(f"/orgs/{org_id}/invites/{created.json()['id']}", headers=bearer(ana)),
            invite(web, ana, org_id, email),
        )
        assert accept.status_code in (200, 410), accept.text
        assert revoke.status_code in (204, 404), revoke.text
        assert reissue.status_code in (201, 409), reissue.text
        joined = email in await members(web, ana, org_id)
        assert joined == (accept.status_code == 200)


async def test_two_owners_cannot_both_step_down_at_once(
    web: httpx.AsyncClient, world: World
) -> None:
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    org_id, ana_id = org["id"], org["user_id"]
    bob, bob_id = await join(web, ana, org_id, world, "bob@example.test")
    promoted = await web.patch(
        f"/orgs/{org_id}/members/{bob_id}", json={"role": "owner"}, headers=bearer(ana)
    )
    assert promoted.status_code == 200, promoted.text
    answers = await asyncio.gather(
        web.patch(f"/orgs/{org_id}/members/{ana_id}", json={"role": "admin"}, headers=bearer(ana)),
        web.patch(f"/orgs/{org_id}/members/{bob_id}", json={"role": "admin"}, headers=bearer(bob)),
    )
    assert sorted(a.status_code for a in answers) == [200, 409]
    roles = [m["role"] for m in (await members(web, ana, org_id)).values()]
    assert roles.count("owner") == 1


# -- regions (Section 17.3) --------------------------------------------------------------------


async def test_a_new_org_is_in_the_home_region_and_another_region_answers_421(
    web: httpx.AsyncClient, engine: AsyncEngine, world: World
) -> None:
    set_home_regions(Regions.of("eu-central-1", None, {"ap-south-1": "https://in.sama.test"}))
    ana = world.person("ana@example.test")
    org = await new_org(web, ana)
    [seen] = await me(web, ana)
    assert (seen["region"], seen["region_here"]) == ("eu-central-1", True)
    assert (await web.get(f"/orgs/{org['id']}/members", headers=bearer(ana))).status_code == 200

    async with engine.begin() as connection:
        await connection.execute(
            update(Org).where(Org.id == uuid.UUID(org["id"])).values(region="ap-south-1")
        )
    for response in (
        await web.get(f"/orgs/{org['id']}/members", headers=bearer(ana)),
        await invite(web, ana, org["id"], "bo@example.test"),
    ):
        assert response.status_code == 421, response.text
        assert response.json()["url"] == "https://in.sama.test"
