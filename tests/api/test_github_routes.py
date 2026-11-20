"""Tests of the GitHub admin routes and the public GitHub setup pages (Section 15.1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from sama.api import JwtVerifier, SamaApi
from sama.db.models import AuditEntry
from sama.github import routes
from sama.github.routes import MAX_FORM_BYTES
from sama.github.service import (
    DEFAULT_PROTECTED_PATHS,
    MAX_PATH_CHARS,
    MAX_PROTECTED_PATHS,
    STATE_TTL_S,
    GitHubService,
)
from tests.api.conftest import ISSUER, SECRET, bearer
from tests.api.test_app import add_user, mia
from tests.github.test_service import CODE, Clock, FakeGitHub, new_installation_id
from tests.orchestrator.conftest import Tenant, add_tenant

pytestmark = pytest.mark.usefixtures("tenant")


def make_api(engine: AsyncEngine, service: GitHubService | None) -> SamaApi:
    return SamaApi(
        engine,
        verifier=JwtVerifier(secret=SecretStr(SECRET), issuer=ISSUER),
        github=service,
    )


async def client_of(api: SamaApi) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://sama.test") as client:
        yield client


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(engine: AsyncEngine, github: FakeGitHub, clock: Clock) -> GitHubService:
    return GitHubService(engine, github.as_client(), state_key=b"k" * 32, clock=clock)


@pytest.fixture
async def http(engine: AsyncEngine, service: GitHubService) -> AsyncIterator[httpx.AsyncClient]:
    async for client in client_of(make_api(engine, service)):
        yield client


@pytest.fixture
async def off(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """An app that runs without the GitHub App."""
    async for client in client_of(make_api(engine, None)):
        yield client


@pytest.fixture
async def admin(engine: AsyncEngine, tenant: Tenant) -> dict[str, str]:
    return bearer(await add_user(engine, tenant, workspace_member=True, role="admin"))


def state_from(url: str) -> str:
    return parse_qs(urlsplit(url).query)["state"][0]


async def install(
    http: httpx.AsyncClient,
    github: FakeGitHub,
    tenant: Tenant,
    admin: dict[str, str],
    *,
    login: str = "acme",
) -> int:
    """Runs the install of a new installation through the routes. Returns its GitHub id."""
    installation_id = new_installation_id()
    github.install(installation_id, login=login)
    response = await http.post(f"/orgs/{tenant.org}/github/install", headers=admin)
    assert response.status_code == 200, response.text
    state = state_from(response.json()["url"])
    page = await http.get(
        "/github/setup",
        params={
            "installation_id": installation_id,
            "setup_action": "install",
            "code": CODE,
            "state": state,
        },
    )
    assert page.status_code == 200, page.text
    assert "Confirm the GitHub connection" in page.text
    done = await confirm(http, state, installation_id)
    assert done.status_code == 200, done.text
    return installation_id


async def confirm(
    http: httpx.AsyncClient, state: str, installation_id: int, code: str = CODE
) -> httpx.Response:
    """Sends the form of the confirm page."""
    return await http.post(
        "/github/setup",
        data={"installation_id": str(installation_id), "code": code, "state": state},
    )


# -- the admin routes ----------------------------------------------------------------------


async def test_every_github_route_answers_409_without_the_app(
    off: httpx.AsyncClient, tenant: Tenant, admin: dict[str, str]
) -> None:
    base = f"/orgs/{tenant.org}/github"
    some = uuid.uuid4()
    calls = [
        await off.post(f"{base}/install", headers=admin),
        await off.get(f"{base}/installations", headers=admin),
        await off.delete(f"{base}/installations/{some}", headers=admin),
        await off.get(f"{base}/repos", headers=admin),
        await off.post(f"{base}/repos", headers=admin, json={"repo": "acme/app"}),
        await off.delete(f"{base}/repos/{some}", headers=admin),
        await off.get(f"{base}/repos", headers=mia(tenant)),
    ]
    assert [call.status_code for call in calls] == [409] * len(calls)
    assert "off" in calls[0].json()["detail"]


async def test_the_setup_page_says_that_github_is_off(off: httpx.AsyncClient) -> None:
    response = await off.get(
        "/github/setup", params={"installation_id": 1, "code": "c", "state": "s"}
    )
    assert response.status_code == 409
    assert "GitHub is off" in response.text


async def test_only_an_admin_installs_and_changes_the_allowlist(
    http: httpx.AsyncClient, tenant: Tenant, github: FakeGitHub, admin: dict[str, str]
) -> None:
    base = f"/orgs/{tenant.org}/github"
    some = uuid.uuid4()
    member = mia(tenant)
    assert (await http.post(f"{base}/install", headers=member)).status_code == 403
    assert (await http.delete(f"{base}/installations/{some}", headers=member)).status_code == 403
    assert (
        await http.post(f"{base}/repos", headers=member, json={"repo": "acme/app"})
    ).status_code == 403
    assert (await http.delete(f"{base}/repos/{some}", headers=member)).status_code == 403
    assert github.repo_installs == {}, "a refused call does not reach GitHub"


async def test_a_user_outside_the_org_sees_nothing(http: httpx.AsyncClient, tenant: Tenant) -> None:
    base = f"/orgs/{tenant.org}/github"
    stranger = bearer(uuid.uuid4())
    for call in (
        await http.post(f"{base}/install", headers=stranger),
        await http.get(f"{base}/installations", headers=stranger),
        await http.get(f"{base}/repos", headers=stranger),
        await http.post(f"{base}/repos", headers=stranger, json={"repo": "acme/app"}),
    ):
        assert call.status_code == 404
    for call in (
        await http.get(f"{base}/installations"),
        await http.get(f"{base}/repos"),
        await http.post(f"{base}/install"),
    ):
        assert call.status_code in (401, 403)


async def test_another_org_admin_cannot_use_the_org(
    http: httpx.AsyncClient, engine: AsyncEngine, tenant: Tenant
) -> None:
    other = await add_tenant(engine)
    foreign = bearer(await add_user(engine, other, workspace_member=True, role="admin"))
    base = f"/orgs/{tenant.org}/github"
    assert (await http.post(f"{base}/install", headers=foreign)).status_code == 404
    assert (await http.get(f"{base}/repos", headers=foreign)).status_code == 404


async def test_an_admin_gets_an_install_url_for_the_org(
    http: httpx.AsyncClient, tenant: Tenant, service: GitHubService, admin: dict[str, str]
) -> None:
    response = await http.post(f"/orgs/{tenant.org}/github/install", headers=admin)
    assert response.status_code == 200
    url = response.json()["url"]
    assert url.startswith("https://github.com/apps/sama-test/installations/new?state=")
    who = service.read_state(state_from(url))
    assert who.org_id == tenant.org
    assert who.user_id != tenant.ids["mia"], "the state names the admin who asked"


async def test_the_install_flow_lists_and_removes_an_installation(
    http: httpx.AsyncClient,
    tenant: Tenant,
    github: FakeGitHub,
    admin: dict[str, str],
    engine: AsyncEngine,
) -> None:
    base = f"/orgs/{tenant.org}/github/installations"
    assert (await http.get(base, headers=mia(tenant))).json() == []
    installation_id = await install(http, github, tenant, admin)
    listed = (await http.get(base, headers=mia(tenant))).json()
    assert len(listed) == 1, "a member reads the installations"
    assert (listed[0]["installation_id"], listed[0]["account"]) == (installation_id, "acme")
    assert set(listed[0]) == {"id", "installation_id", "account", "account_type", "created_at"}

    assert (await http.delete(f"{base}/{listed[0]['id']}", headers=admin)).status_code == 204
    assert github.forgotten == [installation_id]
    assert (await http.get(base, headers=admin)).json() == []
    assert (await http.delete(f"{base}/{listed[0]['id']}", headers=admin)).status_code == 404
    assert (await http.delete(f"{base}/{uuid.uuid4()}", headers=admin)).status_code == 404
    async with engine.connect() as connection:
        actions = (
            await connection.execute(
                select(AuditEntry.action)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.action.like("github.%"))
                .order_by(AuditEntry.id)
            )
        ).scalars()
        assert list(actions) == ["github.installed", "github.removed"]


async def test_an_admin_adds_lists_and_removes_a_repository(
    http: httpx.AsyncClient, tenant: Tenant, github: FakeGitHub, admin: dict[str, str]
) -> None:
    installation_id = await install(http, github, tenant, admin)
    github.add_repo("acme/app", installation_id, canonical="Acme/App")
    base = f"/orgs/{tenant.org}/github/repos"
    assert (await http.get(base, headers=mia(tenant))).json() == []

    response = await http.post(
        base,
        headers=admin,
        json={"repo": "https://github.com/acme/app", "protected_paths": ["deploy/"]},
    )
    assert response.status_code == 201, response.text
    added = response.json()
    assert added["repo"] == "Acme/App"
    assert added["installation_id"] == installation_id
    assert added["protected_paths"] == [*DEFAULT_PROTECTED_PATHS, "deploy/"]
    assert set(added) == {"id", "repo", "installation_id", "protected_paths"}

    assert (await http.get(base, headers=mia(tenant))).json() == [added], "a member reads the list"
    assert (await http.get(base, headers=admin)).json() == [added]

    assert (await http.delete(f"{base}/{added['id']}", headers=admin)).status_code == 204
    assert (await http.get(base, headers=admin)).json() == []
    assert (await http.delete(f"{base}/{added['id']}", headers=admin)).status_code == 404
    assert (await http.delete(f"{base}/{uuid.uuid4()}", headers=admin)).status_code == 404
    assert github.forgotten == [], "removing a repository keeps the installation"


async def test_adding_a_repository_to_the_allowlist_twice_is_a_conflict(
    http: httpx.AsyncClient, tenant: Tenant, github: FakeGitHub, admin: dict[str, str]
) -> None:
    installation_id = await install(http, github, tenant, admin)
    github.add_repo("acme/app", installation_id)
    url = f"/orgs/{tenant.org}/github/repos"
    assert (await http.post(url, headers=admin, json={"repo": "acme/app"})).status_code == 201
    again = await http.post(url, headers=admin, json={"repo": "ACME/APP"})
    assert again.status_code == 409
    assert "already" in again.json()["detail"]
    assert len((await http.get(url, headers=admin)).json()) == 1


async def test_a_repository_that_the_app_cannot_reach_is_a_conflict(
    http: httpx.AsyncClient, tenant: Tenant, github: FakeGitHub, admin: dict[str, str]
) -> None:
    await install(http, github, tenant, admin)
    github.add_repo("acme/hidden", None)
    response = await http.post(
        f"/orgs/{tenant.org}/github/repos", headers=admin, json={"repo": "acme/hidden"}
    )
    assert response.status_code == 409
    assert "cannot reach acme/hidden" in response.json()["detail"]


async def test_a_bad_repository_body_is_refused(
    http: httpx.AsyncClient, tenant: Tenant, admin: dict[str, str]
) -> None:
    url = f"/orgs/{tenant.org}/github/repos"
    not_a_repo = await http.post(url, headers=admin, json={"repo": "not a repo"})
    assert not_a_repo.status_code == 400
    bad_path = await http.post(
        url, headers=admin, json={"repo": "acme/app", "protected_paths": ["../x"]}
    )
    assert bad_path.status_code == 400
    for body in (
        {},
        {"repo": ""},
        {"repo": "x" * 301},
        {"repo": "acme/app", "protected_paths": ["x" * (MAX_PATH_CHARS + 1)]},
        {"repo": "acme/app", "protected_paths": ["a/"] * (MAX_PROTECTED_PATHS + 1)},
        {"repo": "acme/app", "extra": 1},
    ):
        response = await http.post(url, headers=admin, json=body)
        assert response.status_code == 422, body


async def test_the_ids_in_the_paths_must_be_uuids(
    http: httpx.AsyncClient, tenant: Tenant, admin: dict[str, str]
) -> None:
    base = f"/orgs/{tenant.org}/github"
    assert (await http.delete(f"{base}/repos/not-a-uuid", headers=admin)).status_code == 422
    assert (await http.delete(f"{base}/installations/1", headers=admin)).status_code == 422


# -- the public pages ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "code", ["<script>alert(1)</script>&x", "abc; touch /tmp/x", "$(id)", "a b", "`id`"]
)
async def test_the_created_page_refuses_a_code_with_other_characters(
    http: httpx.AsyncClient, code: str
) -> None:
    """The page shows a shell command, so a code must not add a second command."""
    response = await http.get("/github/app/created", params={"code": code})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "<script>" not in response.text
    assert "github-app convert" not in response.text


async def test_the_created_page_shows_a_plain_code_in_the_command(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get("/github/app/created", params={"code": "abc123DEF"})
    assert response.status_code == 200
    assert "<pre>python -m sama.admin github-app convert abc123DEF</pre>" in response.text
    assert "<title>sama: GitHub</title>" in response.text


async def test_the_created_page_needs_a_code_of_a_sane_size(http: httpx.AsyncClient) -> None:
    assert (await http.get("/github/app/created")).status_code == 422
    assert (await http.get("/github/app/created", params={"code": ""})).status_code == 422
    assert (await http.get("/github/app/created", params={"code": "c" * 201})).status_code == 422


async def test_the_created_page_works_without_the_app_service(off: httpx.AsyncClient) -> None:
    assert (await off.get("/github/app/created", params={"code": "abc"})).status_code == 200


async def test_the_setup_page_for_a_request_asks_for_an_owner(http: httpx.AsyncClient) -> None:
    response = await http.get("/github/setup", params={"setup_action": "request"})
    assert response.status_code == 200
    assert "An owner must approve the App" in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"installation_id": 1, "code": "c"},
        {"installation_id": 1, "state": "s"},
        {"code": "c", "state": "s"},
        {"installation_id": 1, "code": "", "state": "s"},
        {"installation_id": 1, "code": "c", "state": ""},
        {"installation_id": 1, "setup_action": "install"},
    ],
)
async def test_the_setup_page_needs_all_the_parameters(
    http: httpx.AsyncClient, github: FakeGitHub, params: dict[str, str | int]
) -> None:
    response = await http.get("/github/setup", params=params)
    assert response.status_code == 400
    assert "Start the install from sama" in response.text
    assert github.visible == {}


@pytest.mark.parametrize("installation_id", ["0", "-1", "abc"])
async def test_the_setup_page_refuses_a_bad_installation_id(
    http: httpx.AsyncClient, installation_id: str
) -> None:
    response = await http.get(
        "/github/setup", params={"installation_id": installation_id, "code": "c", "state": "s"}
    )
    assert response.status_code == 422


async def test_the_setup_page_shows_the_error_status_of_the_service(
    http: httpx.AsyncClient,
    tenant: Tenant,
    github: FakeGitHub,
    clock: Clock,
    admin: dict[str, str],
    engine: AsyncEngine,
) -> None:
    async def start() -> str:
        response = await http.post(f"/orgs/{tenant.org}/github/install", headers=admin)
        return state_from(response.json()["url"])

    async def finish(state: str, installation_id: int, code: str = CODE) -> httpx.Response:
        return await confirm(http, state, installation_id, code)

    seen = new_installation_id()
    github.install(seen)
    unseen = new_installation_id()
    github.installs[unseen] = {"account": {"login": "x", "type": "User"}}

    bad_state = await finish("forged.state", seen)
    assert bad_state.status_code == 400
    assert "The install did not complete" in bad_state.text
    assert "the install link is not valid" in bad_state.text

    cannot_see = await finish(await start(), unseen)
    assert cannot_see.status_code == 403
    assert "cannot see this installation" in cannot_see.text

    old = await start()
    clock.now += STATE_TTL_S + 1
    expired = await finish(old, seen)
    assert expired.status_code == 400
    assert "expired" in expired.text

    other = await add_tenant(engine)
    other_admin = bearer(await add_user(engine, other, workspace_member=True, role="admin"))
    assert (await finish(await start(), seen)).status_code == 200
    taken = await http.post(f"/orgs/{other.org}/github/install", headers=other_admin)
    conflict = await finish(state_from(taken.json()["url"]), seen)
    assert conflict.status_code == 409
    assert "another sama org" in conflict.text


async def test_the_setup_page_shows_that_github_is_connected(
    http: httpx.AsyncClient,
    tenant: Tenant,
    github: FakeGitHub,
    admin: dict[str, str],
) -> None:
    installation_id = new_installation_id()
    github.install(installation_id, login="<b>acme</b>")
    state = state_from(
        (await http.post(f"/orgs/{tenant.org}/github/install", headers=admin)).json()["url"]
    )
    preview = await http.get(
        "/github/setup",
        params={
            "installation_id": installation_id,
            "setup_action": "install",
            "code": CODE,
            "state": state,
        },
    )
    assert preview.status_code == 200
    assert "<h1>Confirm the GitHub connection</h1>" in preview.text
    assert "&lt;b&gt;acme&lt;/b&gt;" in preview.text
    assert "<b>acme</b>" not in preview.text
    assert '<form method="post" action="/github/setup">' in preview.text
    assert github.asked_orgs == [], "the GET does not use the OAuth code"
    assert (await http.get(f"/orgs/{tenant.org}/github/installations", headers=admin)).json() == []

    response = await confirm(http, state, installation_id)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "<h1>GitHub is connected</h1>" in response.text
    assert "&lt;b&gt;acme&lt;/b&gt;" in response.text
    assert CODE not in response.text
    assert state not in response.text
    listed = (await http.get(f"/orgs/{tenant.org}/github/installations", headers=admin)).json()
    assert [row["installation_id"] for row in listed] == [installation_id]


@pytest.mark.parametrize(
    "form",
    [
        {},
        {"installation_id": "x", "code": CODE, "state": "s"},
        {"installation_id": "1", "code": "", "state": "s"},
        {"installation_id": "1", "code": CODE},
        {"installation_id": "1", "code": "c" * 201, "state": "s"},
        {"installation_id": "\u00b2", "code": CODE, "state": "s"},
        {"installation_id": "0", "code": CODE, "state": "s"},
        {"installation_id": "1" * 19, "code": CODE, "state": "s"},
        {"installation_id": "1", "code": CODE, "state": "s", **{f"f{i}": "x" for i in range(9)}},
    ],
)
async def test_the_confirm_form_needs_all_the_fields(
    http: httpx.AsyncClient, form: dict[str, str]
) -> None:
    response = await http.post("/github/setup", data=form)
    assert response.status_code == 400
    assert "Start the install from sama" in response.text


async def test_the_confirm_form_answers_409_without_the_app(off: httpx.AsyncClient) -> None:
    response = await off.post("/github/setup", data={"installation_id": "1"})
    assert response.status_code == 409


async def test_the_confirm_form_refuses_a_large_body(http: httpx.AsyncClient) -> None:
    async def parts() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield b"a" * 1024

    response = await http.post(
        "/github/setup",
        content=parts(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    assert "Start the install from sama" in response.text


async def test_the_body_reader_stops_at_the_limit() -> None:
    """The page is public. A body without a length must not fill the memory, so the reader
    stops at the limit and does not read the other parts."""
    read = 0

    async def receive() -> dict[str, object]:
        nonlocal read
        read += 1
        return {"type": "http.request", "body": b"a" * 1024, "more_body": read < 1000}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    assert await routes._small_body(request) is None
    assert read == MAX_FORM_BYTES // 1024 + 1

    async def short() -> dict[str, object]:
        return {"type": "http.request", "body": b"a=1", "more_body": False}

    assert await routes._small_body(Request({"type": "http", "headers": []}, short)) == b"a=1"
