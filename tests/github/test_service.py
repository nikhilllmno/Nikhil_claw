"""Tests of the GitHub service: the install state, the installations, the allowlist, and the
push grants (docs/architecture.md, Section 15.1). A fake client takes the place of GitHub."""

# ruff: noqa: F811 -- the fixtures of the tests are imported, and a test names them as arguments
from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.db.models import AuditEntry, GitHubInstallation, GitHubPushGrant, GitHubRepo, Org, Run
from sama.github.client import GitHubAppConfig, GitHubClient, GitHubError
from sama.github.service import (
    DEFAULT_PROTECTED_PATHS,
    MAX_PATH_CHARS,
    MAX_PROTECTED_PATHS,
    PUSH_GRANT_TTL,
    STATE_TTL_S,
    AllowedRepo,
    GitHubService,
    GitHubSetupError,
    is_protected,
    normalize_paths,
    split_repo,
    valid_sama_branch,
)
from tests.orchestrator.conftest import (  # noqa: F401
    Tenant,
    add_tenant,
    engine,
    queue_database_url,
    tenant,
)

CODE = "oauth-code"
SHA = "a" * 40
REF = "refs/heads/sama/work"
NOW = 1_800_000_000.0


class FakeGitHub:
    """The methods of ``GitHubClient`` that the service calls, without a network."""

    def __init__(self) -> None:
        self.config = GitHubAppConfig(
            app_id="1",
            slug="sama-test",
            client_id="Iv1.test",
            private_key=SecretStr("not-a-key"),
            client_secret=SecretStr("not-a-secret"),
        )
        self.visible: dict[str, set[int]] = {}
        self.code_errors: dict[str, GitHubError] = {}
        """The installations that each OAuth code can see."""
        self.installs: dict[int, dict[str, Any]] = {}
        """The answer of ``installation`` for each installation id."""
        self.repo_installs: dict[str, int | GitHubError | None] = {}
        """The installation of each repository (key in lower case)."""
        self.repos: dict[str, dict[str, Any]] = {}
        """The answer of ``repo`` for each repository (key in lower case)."""
        self.forgotten: list[int] = []

    def as_client(self) -> GitHubClient:
        return self  # type: ignore[return-value]

    def install(
        self, installation_id: int, login: str = "acme", kind: str = "Organization"
    ) -> None:
        self.installs[installation_id] = {"account": {"login": login, "type": kind}}
        self.visible.setdefault(CODE, set()).add(installation_id)

    def add_repo(self, full_name: str, installation_id: int | None, canonical: str = "") -> None:
        key = full_name.lower()
        self.repo_installs[key] = installation_id
        owner, _, name = (canonical or full_name).partition("/")
        self.repos[key] = {"owner": {"login": owner}, "name": name}

    async def user_installation_ids(self, code: str) -> set[int]:
        if code in self.code_errors:
            raise self.code_errors[code]
        return self.visible.get(code, set())

    async def installation(self, installation_id: int) -> dict[str, Any]:
        return self.installs[installation_id]

    async def repo_installation(self, owner: str, name: str) -> int | None:
        found = self.repo_installs.get(f"{owner}/{name}".lower())
        if isinstance(found, GitHubError):
            raise found
        return found

    async def repo(self, installation_id: int, owner: str, name: str) -> dict[str, Any]:
        return self.repos[f"{owner}/{name}".lower()]

    def forget(self, installation_id: int) -> None:
        self.forgotten.append(installation_id)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(
    engine: AsyncEngine,
    github: FakeGitHub,
    clock: Clock,
) -> GitHubService:
    return GitHubService(engine, github.as_client(), state_key=b"k" * 32, clock=clock)


@pytest.fixture
async def other(engine: AsyncEngine) -> AsyncIterator[Tenant]:
    second = await add_tenant(engine)
    yield second
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL sama.audit_purge = 'on'"))
        await connection.execute(delete(Org).where(Org.id == second.org))


def new_installation_id() -> int:
    return uuid.uuid4().int % 10**9 + 1


def state_of(service: GitHubService, tenant: Tenant) -> str:
    """The state that the install URL of ``service`` carries."""
    url = service.install_url(tenant.org, tenant.ids["mia"])
    assert url.startswith("https://github.com/apps/sama-test/installations/new?state=")
    return url.partition("state=")[2]


async def connect(
    service: GitHubService, github: FakeGitHub, tenant: Tenant, installation_id: int | None = None
) -> int:
    installation_id = installation_id or new_installation_id()
    github.install(installation_id)
    await service.finish_install(state_of(service, tenant), installation_id, CODE)
    return installation_id


async def actions(engine: AsyncEngine, org: uuid.UUID, prefix: str = "github.") -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEntry.action)
            .where(AuditEntry.org_id == org, AuditEntry.action.like(f"{prefix}%"))
            .order_by(AuditEntry.id)
        )
        return list(rows.scalars())


async def count(engine: AsyncEngine, model: Any, org: uuid.UUID) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                select(func.count()).select_from(model).where(model.org_id == org)
            )
            or 0
        )


# -- the pure helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text_",
    [
        "acme/app",
        " acme/app ",
        "acme/app/",
        "acme/app.git",
        "https://github.com/acme/app",
        "https://github.com/acme/app.git",
        "http://github.com/acme/app/",
        "github.com/acme/app",
        "HTTPS://GitHub.com/acme/app",
    ],
)
def test_split_repo_reads_the_known_forms(text_: str) -> None:
    assert split_repo(text_) == ("acme", "app")


def test_split_repo_keeps_dots_and_case_in_the_name() -> None:
    assert split_repo("Acme-Co/My_repo.js") == ("Acme-Co", "My_repo.js")


@pytest.mark.parametrize(
    "text_",
    [
        "",
        "acme",
        "acme/",
        "/app",
        "-acme/app",
        "ac me/app",
        "acme/a b",
        "acme/.",
        "acme/..",
        "acme/app/extra",
        "acme/app?x=1",
        "a" * 40 + "/app",
        "acme/" + "n" * 101,
    ],
)
def test_split_repo_refuses_other_text(text_: str) -> None:
    with pytest.raises(GitHubSetupError) as caught:
        split_repo(text_)
    assert caught.value.status_code == 400


@pytest.mark.parametrize(
    "branch", ["sama/work", "sama/feature/x-1", "sama/a.b_c", "sama/x.lock.y", "sama/" + "a" * 100]
)
def test_valid_sama_branches(branch: str) -> None:
    assert valid_sama_branch(branch)


@pytest.mark.parametrize(
    "branch",
    [
        "",
        "sama/",
        "main",
        "Sama/work",
        "sama",
        "feature/sama/x",
        "sama/a..b",
        "sama/a//b",
        "sama/a/",
        "sama/a.",
        "sama/a.lock",
        "sama/a/.hidden",
        "sama/a b",
        "sama/a~1",
        "sama/a^",
        "sama/a:b",
        "sama/a\\b",
        "sama/" + "a" * 200,
    ],
)
def test_invalid_sama_branches(branch: str) -> None:
    assert not valid_sama_branch(branch)


def test_normalize_paths_cleans_and_removes_duplicates() -> None:
    assert normalize_paths([" ./ci/ ", "ci/", "deploy/prod.yml", "./deploy/prod.yml"]) == (
        "ci/",
        "deploy/prod.yml",
    )
    assert normalize_paths([]) == ()


@pytest.mark.parametrize(
    "path",
    [
        "",
        "   ",
        "/etc/passwd",
        "../x",
        "a/../b",
        "a\\b",
        "a\nb",
        "a\x00b",
        "x" * (MAX_PATH_CHARS + 1),
    ],
)
def test_normalize_paths_refuses_a_bad_path(path: str) -> None:
    with pytest.raises(GitHubSetupError):
        normalize_paths(["ok/", path])


def test_normalize_paths_limits() -> None:
    assert normalize_paths(["x" * MAX_PATH_CHARS]) == ("x" * MAX_PATH_CHARS,)
    assert normalize_paths([f"p{i}/" for i in range(MAX_PROTECTED_PATHS)])
    with pytest.raises(GitHubSetupError, match="at most"):
        normalize_paths([f"p{i}/" for i in range(MAX_PROTECTED_PATHS + 1)])
    # The same path many times is one path.
    assert normalize_paths(["a/"] * (MAX_PROTECTED_PATHS + 10)) == ("a/",)
    assert normalize_paths(["a..b/c"]) == ("a..b/c",), "a name with .. is fine"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".GitHub/Workflows/CI.yml",
        ".github/actions/setup/action.yml",
        ".github/CODEOWNERS",
        ".github/codeowners",
        "CODEOWNERS",
        "codeowners",
        "docs/CODEOWNERS",
        "DOCS/codeowners",
    ],
)
def test_default_protected_paths(path: str) -> None:
    assert is_protected(path, DEFAULT_PROTECTED_PATHS)


@pytest.mark.parametrize(
    "path",
    [
        "src/main.py",
        "CODEOWNERS.bak",
        "docs/CODEOWNERS.md",
        ".github/CODEOWNERS2",
        ".github/workflows",
        ".github/other.yml",
        "x/.github/workflows/ci.yml",
        "README.md",
    ],
)
def test_other_paths_are_not_protected(path: str) -> None:
    assert not is_protected(path, DEFAULT_PROTECTED_PATHS)


def test_a_folder_rule_ends_with_a_slash_and_a_file_rule_is_exact() -> None:
    assert is_protected("Deploy/prod/a.yml", ["deploy/"])
    assert not is_protected("deployment/a.yml", ["deploy/"])
    assert not is_protected("deploy", ["deploy/"]), "a file named like the folder"
    assert is_protected("DEPLOY.YML", ["deploy.yml"])
    assert not is_protected("deploy.yml.bak", ["deploy.yml"])
    assert not is_protected("deploy.yml2", ["deploy.yml"])
    assert not is_protected("a.py", [])


def test_a_file_rule_also_covers_a_folder_of_that_name() -> None:
    assert is_protected("CODEOWNERS/x", ["CODEOWNERS"])


def test_allowed_repo_public_view() -> None:
    repo = AllowedRepo(uuid.uuid4(), uuid.uuid4(), 5, "Acme", "App", ("ci/",))
    assert repo.full_name == "Acme/App"
    assert repo.protected_paths == (*DEFAULT_PROTECTED_PATHS, "ci/")
    assert repo.public() == {
        "id": str(repo.id),
        "repo": "Acme/App",
        "installation_id": 5,
        "protected_paths": [*DEFAULT_PROTECTED_PATHS, "ci/"],
    }


# -- the install state ---------------------------------------------------------------------


def test_the_state_round_trips(service: GitHubService, tenant: Tenant) -> None:
    state = state_of(service, tenant)
    who = service.read_state(state)
    assert (who.org_id, who.user_id) == (tenant.org, tenant.ids["mia"])


def test_the_state_is_url_safe(service: GitHubService, tenant: Tenant) -> None:
    state = state_of(service, tenant)
    assert all(char.isalnum() or char in "-_." for char in state), state


def test_a_state_of_another_key_is_refused(
    engine: AsyncEngine,
    github: FakeGitHub,
    clock: Clock,
    tenant: Tenant,
    service: GitHubService,
) -> None:
    state = state_of(service, tenant)
    stranger = GitHubService(engine, github.as_client(), state_key=b"z" * 32, clock=clock)
    with pytest.raises(GitHubSetupError, match="not valid"):
        stranger.read_state(state)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def test_a_tampered_state_is_refused(service: GitHubService, tenant: Tenant) -> None:
    state = state_of(service, tenant)
    raw, _, signature = state.partition(".")
    payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    payload["o"] = str(uuid.uuid4())
    forged = _b64(json.dumps(payload, separators=(",", ":")).encode())
    for bad in (
        f"{forged}.{signature}",  # another org with the old signature
        f"{raw}.{'A' * len(signature)}",  # another signature
        raw,  # no signature
        f"{raw}.",
        f".{signature}",
        "",
        "not a state",
        f"{raw}.{signature}.extra",
        f"{raw}x.{signature}",
    ):
        with pytest.raises(GitHubSetupError, match="not valid"):
            service.read_state(bad)


def test_a_signed_state_with_a_bad_payload_is_refused(service: GitHubService) -> None:
    for payload in (b"[]", b"{}", b"not json", b'{"o": "x", "u": "y", "e": 9999999999}'):
        raw = _b64(payload)
        state = f"{raw}.{_b64(service._sign(raw))}"
        with pytest.raises(GitHubSetupError, match="not valid"):
            service.read_state(state)


def test_the_state_expires(service: GitHubService, clock: Clock, tenant: Tenant) -> None:
    state = state_of(service, tenant)
    clock.now = NOW + STATE_TTL_S
    assert service.read_state(state).org_id == tenant.org, "the last second counts"
    clock.now = NOW + STATE_TTL_S + 1
    with pytest.raises(GitHubSetupError, match="expired") as caught:
        service.read_state(state)
    assert caught.value.status_code == 400


# -- the installations ---------------------------------------------------------------------


async def test_an_install_links_the_installation_to_the_org(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = new_installation_id()
    github.install(installation_id, login="Acme", kind="Organization")
    linked = await service.finish_install(state_of(service, tenant), installation_id, CODE)
    assert linked == {
        "org_id": str(tenant.org),
        "installation_id": installation_id,
        "account": "Acme",
        "account_type": "Organization",
    }
    listed = await service.installations(tenant.org)
    assert len(listed) == 1
    assert listed[0]["installation_id"] == installation_id
    assert (listed[0]["account"], listed[0]["account_type"]) == ("Acme", "Organization")
    async with engine.connect() as connection:
        stored = (
            await connection.execute(
                select(GitHubInstallation).where(GitHubInstallation.org_id == tenant.org)
            )
        ).one()
    assert stored.created_by_user_id == tenant.ids["mia"]
    async with engine.connect() as connection:
        entry = (
            await connection.execute(
                select(AuditEntry).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "github.installed"
                )
            )
        ).one()
    assert (entry.actor_kind, entry.actor_user_id) == ("user", tenant.ids["mia"])
    assert entry.target_type == "github_installation"
    assert entry.target_id == str(stored.id)
    assert entry.details == {"installation_id": installation_id, "account": "Acme"}


async def test_the_oauth_check_refuses_an_installation_that_the_person_cannot_see(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    mine, theirs = new_installation_id(), new_installation_id()
    github.install(mine)
    github.install(theirs)
    github.visible[CODE] = {mine}
    state = state_of(service, tenant)
    with pytest.raises(GitHubSetupError) as caught:
        await service.finish_install(state, theirs, CODE)
    assert caught.value.status_code == 403
    with pytest.raises(GitHubSetupError) as caught:
        await service.finish_install(state, mine, "another-code")
    assert caught.value.status_code == 403, "a code that sees nothing"
    assert await service.installations(tenant.org) == []
    assert await actions(engine, tenant.org) == []


async def test_a_failed_oauth_exchange_is_a_bad_request(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    github.code_errors["bad"] = GitHubError("GitHub did not accept the code", 400)
    with pytest.raises(GitHubSetupError, match="did not accept the code") as caught:
        await service.finish_install(state_of(service, tenant), 7, "bad")
    assert caught.value.status_code == 400


async def test_a_bad_state_stops_the_install_before_any_github_call(
    service: GitHubService, github: FakeGitHub, clock: Clock, tenant: Tenant
) -> None:
    installation_id = new_installation_id()
    github.install(installation_id)
    state = state_of(service, tenant)
    clock.now += STATE_TTL_S + 1
    with pytest.raises(GitHubSetupError, match="expired"):
        await service.finish_install(state, installation_id, CODE)
    with pytest.raises(GitHubSetupError, match="not valid"):
        await service.finish_install("x.y", installation_id, CODE)
    assert await service.installations(tenant.org) == []


async def test_an_installation_without_an_account_is_refused(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    installation_id = new_installation_id()
    github.visible[CODE] = {installation_id}
    github.installs[installation_id] = {"account": None}
    with pytest.raises(GitHubSetupError) as caught:
        await service.finish_install(state_of(service, tenant), installation_id, CODE)
    assert caught.value.status_code == 502


async def test_a_second_install_updates_the_row(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    first = (await service.installations(tenant.org))[0]
    github.install(installation_id, login="renamed", kind="User")
    await service.finish_install(state_of(service, tenant), installation_id, CODE)
    listed = await service.installations(tenant.org)
    assert len(listed) == 1
    assert listed[0]["id"] == first["id"], "the upsert keeps the row"
    assert (listed[0]["account"], listed[0]["account_type"]) == ("renamed", "User")
    assert await actions(engine, tenant.org) == ["github.installed", "github.installed"]


async def test_another_org_cannot_take_an_installation(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    other: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    with pytest.raises(GitHubSetupError, match="another sama org") as caught:
        await service.finish_install(state_of(service, other), installation_id, CODE)
    assert caught.value.status_code == 409
    assert await service.installations(other.org) == []
    assert len(await service.installations(tenant.org)) == 1
    assert await actions(engine, other.org) == []


async def test_the_installations_of_an_org_are_listed_in_order_for_that_org_only(
    service: GitHubService, github: FakeGitHub, tenant: Tenant, other: Tenant
) -> None:
    first = await connect(service, github, tenant)
    second = await connect(service, github, tenant)
    await connect(service, github, other)
    listed = await service.installations(tenant.org)
    assert [row["installation_id"] for row in listed] == [first, second]
    assert len(await service.installations(other.org)) == 1


async def test_removing_an_installation_removes_its_repos_and_forgets_its_tokens(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    other: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    kept = await connect(service, github, tenant)
    github.add_repo("acme/one", installation_id)
    github.add_repo("acme/two", kept)
    await service.add_repo(tenant.org, tenant.ids["mia"], "acme/one")
    await service.add_repo(tenant.org, tenant.ids["mia"], "acme/two")
    row_id = uuid.UUID(
        next(
            row["id"]
            for row in await service.installations(tenant.org)
            if row["installation_id"] == installation_id
        )
    )
    # Another org cannot remove it.
    assert not await service.remove_installation(other.org, other.ids["mia"], row_id)
    assert github.forgotten == []
    assert await service.remove_installation(tenant.org, tenant.ids["mia"], row_id)
    assert github.forgotten == [installation_id]
    assert [row["installation_id"] for row in await service.installations(tenant.org)] == [kept]
    assert [repo.full_name for repo in await service.repos(tenant.org)] == ["acme/two"]
    assert await service.allowed(tenant.org, "acme", "one") is None
    assert not await service.remove_installation(tenant.org, tenant.ids["mia"], row_id)
    assert github.forgotten == [installation_id], "a missing row forgets nothing"
    assert await actions(engine, tenant.org) == [
        "github.installed",
        "github.installed",
        "github.repo_added",
        "github.repo_added",
        "github.removed",
    ]
    async with engine.connect() as connection:
        entry = (
            await connection.execute(
                select(AuditEntry).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "github.removed"
                )
            )
        ).one()
    assert entry.target_id == str(row_id)
    assert entry.details == {"installation_id": installation_id}


# -- the allowlist -------------------------------------------------------------------------


async def test_an_admin_adds_a_repository(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id, canonical="Acme/App")
    repo = await service.add_repo(
        tenant.org, tenant.ids["mia"], "https://github.com/acme/app.git", [" ./ci/ "]
    )
    assert (repo.owner, repo.name, repo.full_name) == ("Acme", "App", "Acme/App")
    assert (repo.org_id, repo.installation_id) == (tenant.org, installation_id)
    assert repo.extra_paths == ("ci/",)
    assert repo.protected_paths == (*DEFAULT_PROTECTED_PATHS, "ci/")
    assert [found.id for found in await service.repos(tenant.org)] == [repo.id]
    async with engine.connect() as connection:
        stored = (
            await connection.execute(select(GitHubRepo).where(GitHubRepo.id == repo.id))
        ).one()
        entry = (
            await connection.execute(
                select(AuditEntry).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "github.repo_added"
                )
            )
        ).one()
    assert stored.full_name == "acme/app", "the key is in lower case"
    assert stored.protected_paths == ["ci/"]
    assert stored.created_by_user_id == tenant.ids["mia"]
    assert entry.target_id == str(repo.id)
    assert entry.details == {"repo": "Acme/App", "protected_paths": ["ci/"]}


async def test_the_allowlist_ignores_case(
    service: GitHubService, github: FakeGitHub, tenant: Tenant, other: Tenant
) -> None:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id, canonical="Acme/App")
    added = await service.add_repo(tenant.org, tenant.ids["mia"], "ACME/APP")
    for owner, name in (("acme", "app"), ("Acme", "App"), ("ACME", "aPp")):
        found = await service.allowed(tenant.org, owner, name)
        assert found is not None
        assert (found.id, found.full_name) == (added.id, "Acme/App")
        assert found.installation_id == installation_id
    assert await service.allowed(tenant.org, "acme", "other") is None
    assert await service.allowed(other.org, "acme", "app") is None, "the allowlist is per org"


async def test_the_repo_keeps_the_name_that_the_user_typed_when_github_gives_no_name(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    installation_id = await connect(service, github, tenant)
    github.repo_installs["acme/app"] = installation_id
    github.repos["acme/app"] = {}
    repo = await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert repo.full_name == "acme/app"


async def test_a_repository_that_the_app_cannot_reach_is_refused(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    await connect(service, github, tenant)
    github.add_repo("acme/nope", None)
    with pytest.raises(GitHubSetupError, match="cannot reach acme/nope") as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/nope")
    assert caught.value.status_code == 409
    assert await service.repos(tenant.org) == []
    assert await actions(engine, tenant.org, "github.repo") == []


async def test_a_repository_of_an_installation_of_another_org_is_refused(
    service: GitHubService, github: FakeGitHub, tenant: Tenant, other: Tenant
) -> None:
    foreign = await connect(service, github, other)
    await connect(service, github, tenant)
    github.add_repo("acme/app", foreign)
    with pytest.raises(GitHubSetupError) as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert caught.value.status_code == 409
    assert await service.repos(tenant.org) == []


async def test_an_org_without_an_installation_cannot_add_a_repository(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    github.add_repo("acme/app", new_installation_id())
    with pytest.raises(GitHubSetupError) as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert caught.value.status_code == 409


async def test_a_repository_is_added_once(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id, canonical="Acme/App")
    await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    with pytest.raises(GitHubSetupError, match="already") as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "ACME/app")
    assert caught.value.status_code == 409
    assert len(await service.repos(tenant.org)) == 1
    assert await actions(engine, tenant.org, "github.repo") == ["github.repo_added"]


async def test_two_orgs_can_add_the_same_repository_with_their_own_installations(
    service: GitHubService, github: FakeGitHub, tenant: Tenant, other: Tenant
) -> None:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id)
    await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    second = await connect(service, github, other)
    github.add_repo("acme/app", second)
    await service.add_repo(other.org, other.ids["mia"], "acme/app")
    found = await service.allowed(other.org, "acme", "app")
    assert found is not None
    assert found.installation_id == second


async def test_github_errors_become_a_bad_gateway(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    installation_id = await connect(service, github, tenant)
    github.repo_installs["acme/app"] = GitHubError("GitHub is down", 503)
    with pytest.raises(GitHubSetupError, match="down") as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert caught.value.status_code == 502

    class Broken(FakeGitHub):
        async def repo(self, installation_id: int, owner: str, name: str) -> dict[str, Any]:
            raise GitHubError("no access", 403)

    broken = Broken()
    broken.repo_installs["acme/app"] = installation_id
    service.client = broken.as_client()
    with pytest.raises(GitHubSetupError, match="no access") as caught:
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert caught.value.status_code == 502


async def test_a_bad_repository_name_or_path_stops_before_any_github_call(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    with pytest.raises(GitHubSetupError):
        await service.add_repo(tenant.org, tenant.ids["mia"], "not a repo")
    with pytest.raises(GitHubSetupError):
        await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app", ["../x"])


async def test_removing_a_repository(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    other: Tenant,
    engine: AsyncEngine,
) -> None:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id, canonical="Acme/App")
    repo = await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert not await service.remove_repo(other.org, other.ids["mia"], repo.id)
    assert len(await service.repos(tenant.org)) == 1
    assert await service.remove_repo(tenant.org, tenant.ids["mia"], repo.id)
    assert await service.repos(tenant.org) == []
    assert await service.allowed(tenant.org, "acme", "app") is None
    assert not await service.remove_repo(tenant.org, tenant.ids["mia"], repo.id)
    assert github.forgotten == [], "the installation stays"
    assert len(await service.installations(tenant.org)) == 1
    async with engine.connect() as connection:
        entry = (
            await connection.execute(
                select(AuditEntry).where(
                    AuditEntry.org_id == tenant.org, AuditEntry.action == "github.repo_removed"
                )
            )
        ).one()
    assert (entry.target_type, entry.target_id) == ("github_repo", str(repo.id))
    assert entry.details == {"repo": "acme/app"}
    assert await actions(engine, tenant.org, "github.repo") == [
        "github.repo_added",
        "github.repo_removed",
    ]
    # The same repository can be added again after the removal.
    again = await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")
    assert again.id != repo.id


async def test_the_repos_of_an_org_are_listed_by_name(
    service: GitHubService, github: FakeGitHub, tenant: Tenant
) -> None:
    installation_id = await connect(service, github, tenant)
    for name in ("acme/zeta", "acme/alpha", "beta/mid"):
        github.add_repo(name, installation_id)
        await service.add_repo(tenant.org, tenant.ids["mia"], name)
    assert [repo.full_name for repo in await service.repos(tenant.org)] == [
        "acme/alpha",
        "acme/zeta",
        "beta/mid",
    ]


# -- the push grants -----------------------------------------------------------------------


async def make_run(engine: AsyncEngine, tenant: Tenant) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            insert(Run).values(
                id=run_id,
                org_id=tenant.org,
                session_id=tenant.ids["s1"],
                agent_profile_id=tenant.ids["profile"],
                runtime="fake",
            )
        )
    return run_id


async def granted_repo(service: GitHubService, github: FakeGitHub, tenant: Tenant) -> AllowedRepo:
    installation_id = await connect(service, github, tenant)
    github.add_repo("acme/app", installation_id)
    return await service.add_repo(tenant.org, tenant.ids["mia"], "acme/app")


async def test_a_push_grant_is_used_once(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    before = datetime.now(UTC)
    expires = await service.grant_push(
        repo, REF, SHA, session_id=tenant.ids["s1"], run_id=run_id, user_id=tenant.ids["mia"]
    )
    after = datetime.now(UTC)
    assert before + PUSH_GRANT_TTL <= expires <= after + PUSH_GRANT_TTL
    assert timedelta(hours=1) == PUSH_GRANT_TTL
    assert await service.take_grant(repo, REF, SHA) is True
    assert await service.take_grant(repo, REF, SHA) is False, "a grant is single use"
    async with engine.connect() as connection:
        grant = (
            await connection.execute(
                select(GitHubPushGrant).where(GitHubPushGrant.org_id == tenant.org)
            )
        ).one()
    assert grant.used_at is not None
    assert (grant.repo_id, grant.ref, grant.commit_sha) == (repo.id, REF, SHA)
    assert (grant.session_id, grant.run_id, grant.expires_at) == (tenant.ids["s1"], run_id, expires)
    async with engine.connect() as connection:
        entries = (
            await connection.execute(
                select(AuditEntry)
                .where(AuditEntry.org_id == tenant.org, AuditEntry.action.like("github.push_%"))
                .order_by(AuditEntry.id)
            )
        ).all()
    assert [entry.action for entry in entries] == ["github.push_granted", "github.push_grant_used"]
    assert all(entry.actor_kind == "agent" for entry in entries)
    assert all(entry.run_id == run_id for entry in entries)
    assert all(entry.target_id == str(repo.id) for entry in entries)
    assert entries[0].actor_user_id == tenant.ids["mia"]
    assert entries[0].details == {
        "grant_id": str(grant.id),
        "repo": "acme/app",
        "ref": REF,
        "commit": SHA,
    }
    assert entries[1].details == {"grant_id": str(grant.id), "ref": REF, "commit": SHA}


async def test_a_grant_is_for_one_commit_and_one_branch(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    await service.grant_push(
        repo, REF, SHA, session_id=tenant.ids["s1"], run_id=run_id, user_id=None
    )
    assert not await service.take_grant(repo, REF, "b" * 40), "another commit"
    assert not await service.take_grant(repo, "refs/heads/sama/other", SHA), "another branch"
    assert not await service.take_grant(repo, REF.upper(), SHA)
    other_repo = AllowedRepo(uuid.uuid4(), tenant.org, 1, "acme", "x")
    assert not await service.take_grant(other_repo, REF, SHA), "another repository"
    wrong_org = AllowedRepo(repo.id, uuid.uuid4(), 1, "acme", "app")
    assert not await service.take_grant(wrong_org, REF, SHA), "another org"
    assert await service.take_grant(repo, REF, SHA), "the grant is still open"


async def test_an_expired_grant_is_not_used(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    await service.grant_push(
        repo, REF, SHA, session_id=tenant.ids["s1"], run_id=run_id, user_id=None
    )
    async with engine.begin() as connection:
        await connection.execute(
            update(GitHubPushGrant).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert not await service.take_grant(repo, REF, SHA)
    assert await actions(engine, tenant.org, "github.push_grant_used") == []


async def test_each_grant_allows_one_push(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    for _ in range(2):
        await service.grant_push(
            repo, REF, SHA, session_id=tenant.ids["s1"], run_id=run_id, user_id=None
        )
    assert [await service.take_grant(repo, REF, SHA) for _ in range(3)] == [True, True, False]
    assert await count(engine, GitHubPushGrant, tenant.org) == 2


async def test_a_grant_needs_a_sama_branch_and_a_full_commit_id(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    for ref, commit in (
        ("refs/heads/main", SHA),
        ("sama/work", SHA),
        (REF, "abc123"),
        (REF, "A" * 40),
        (REF, SHA + "0"),
    ):
        with pytest.raises(GitHubSetupError):
            await service.grant_push(
                repo, ref, commit, session_id=tenant.ids["s1"], run_id=run_id, user_id=None
            )
    assert await count(engine, GitHubPushGrant, tenant.org) == 0


async def test_removing_a_repository_removes_its_grants(
    service: GitHubService,
    github: FakeGitHub,
    tenant: Tenant,
    engine: AsyncEngine,
) -> None:
    repo = await granted_repo(service, github, tenant)
    run_id = await make_run(engine, tenant)
    await service.grant_push(
        repo, REF, SHA, session_id=tenant.ids["s1"], run_id=run_id, user_id=None
    )
    assert await service.remove_repo(tenant.org, tenant.ids["mia"], repo.id)
    assert await count(engine, GitHubPushGrant, tenant.org) == 0
    assert not await service.take_grant(repo, REF, SHA)
