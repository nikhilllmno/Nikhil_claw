"""``sama_pr``: the agent changes the code of sama itself (docs/architecture.md, Section 15.2)."""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from sama.gate.decide import (
    ALWAYS_ASK,
    DEFAULT_ALLOW,
    NEEDS_A_HUMAN,
    TOOLSETS,
    GateContext,
    GateStep,
    PolicyRule,
    RuleLevel,
    decide,
)
from sama.github.client import GitHubError
from sama.github.codeowners import parse_codeowners
from sama.github.proxy import GitProxy
from sama.github.service import DEFAULT_SELF_REPO, AllowedRepo, GitHubService
from sama.runtime.contract import Audience, RunScope
from sama.sandbox import Sandbox, SandboxSpec
from sama.sandbox.local import LocalProvider
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import MemoryTodoStore, RepoCloner, RepoError, ToolServer
from sama.tools.code import BLOCKED_TOOLS
from sama.tools.sama_pr import (
    MAX_BODY_CHARS,
    MAX_COMPARE_FILES,
    MAX_LISTED_FILES,
    MAX_SUMMARY_CHARS,
    MAX_TESTS_CHARS,
    OWNER_REVIEW_HEADING,
    ChangedFile,
    SamaPr,
    SamaPrError,
    owner_review,
    pull_body,
)
from tests.conformance.scripted_model import serve
from tests.github.support import LocalUpstream, MemoryStore, git
from tests.tools.test_server import connect, text_of

ORG = uuid.uuid4()
CODEOWNERS = "\n".join(
    [
        "# owners",
        "/src/sama/gate/   @owner @acme/security",
        "/.github/         @owner",
        "/docs/            ",
        "",
    ]
)


@dataclass
class FakeClient:
    """A fake GitHub for ``open``."""

    default_branch: str = "main"
    files: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"filename": "src/sama/gate/decide.py", "status": "modified"},
            {"filename": "docs/roadmap.md", "status": "modified"},
            {"filename": "tests/test_x.py", "status": "added"},
        ]
    )
    ahead_by: int = 1
    owners_files: dict[str, str] = field(default_factory=lambda: {".github/CODEOWNERS": CODEOWNERS})
    compare_error: GitHubError | None = None
    reviewers_error: GitHubError | None = None
    pull_error: GitHubError | None = None
    pulls: list[dict[str, Any]] = field(default_factory=list)
    reviewers: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    asked_files: list[tuple[str, str]] = field(default_factory=list)
    compared: list[tuple[str, str]] = field(default_factory=list)

    async def repo(self, installation_id: int, owner: str, name: str) -> Any:
        return {"default_branch": self.default_branch}

    async def compare(
        self, installation_id: int, owner: str, name: str, base: str, head: str
    ) -> Any:
        self.compared.append((base, head))
        if self.compare_error is not None:
            raise self.compare_error
        return {"status": "ahead", "ahead_by": self.ahead_by, "files": self.files}

    async def file_text(
        self, installation_id: int, owner: str, name: str, path: str, ref: str, *, max_bytes: int
    ) -> str | None:
        self.asked_files.append((path, ref))
        text = self.owners_files.get(path)
        if text is not None and len(text.encode()) > max_bytes:
            raise GitHubError(f"{path} has {len(text.encode())} bytes, more than {max_bytes}")
        return text

    async def create_pull(self, installation_id: int, owner: str, name: str, **kw: Any) -> Any:
        if self.pull_error is not None:
            raise self.pull_error
        self.pulls.append({"repo": f"{owner}/{name}", "installation_id": installation_id, **kw})
        return {"number": 12, "html_url": f"https://github.com/{owner}/{name}/pull/12", **kw}

    async def request_reviewers(
        self,
        installation_id: int,
        owner: str,
        name: str,
        number: int,
        *,
        users: tuple[str, ...],
        teams: tuple[str, ...],
    ) -> Any:
        if self.reviewers_error is not None:
            raise self.reviewers_error
        self.reviewers.append((number, tuple(users), tuple(teams)))
        return {}


@dataclass
class FakeService:
    client: FakeClient = field(default_factory=FakeClient)
    store: MemoryStore = field(default_factory=MemoryStore)

    async def allowed(self, org_id: uuid.UUID, owner: str, name: str) -> AllowedRepo | None:
        return await self.store.allowed(org_id, owner, name)


def scope(org: uuid.UUID = ORG) -> RunScope:
    return RunScope(
        org_id=str(org),
        workspace_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        audience=Audience.PRIVATE,
        user_id=str(uuid.uuid4()),
    )


@pytest.fixture
def signer() -> RunTokenSigner:
    return RunTokenSigner(b"k" * 32)


@pytest.fixture
def service() -> FakeService:
    found = FakeService()
    found.store.add(AllowedRepo(uuid.uuid4(), ORG, 77, "nikhilllmno", "Nikhil_claw"))
    return found


@pytest.fixture
def sama_pr(service: FakeService) -> SamaPr:
    return SamaPr(cast(GitHubService, service))


def claims(signer: RunTokenSigner, org: uuid.UUID = ORG) -> RunTokenClaims:
    return signer.verify(signer.issue(scope(org), ttl_s=600))


async def open_pr(sama_pr: SamaPr, signer: RunTokenSigner, **kwargs: Any) -> str:
    args: dict[str, Any] = {
        "branch": "sama/fix-gate",
        "title": "  fix: the gate  ",
        "summary": "Fix the gate.",
        "tests": "uv run pytest: 10 passed",
        "draft": False,
    }
    args.update(kwargs)
    return await sama_pr.open(claims(signer), **args)


# -- open -------------------------------------------------------------------------------------


async def test_open_asks_the_code_owners_of_the_changed_paths(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    text = await open_pr(sama_pr, signer)
    client = service.client
    assert client.compared == [("main", "sama/fix-gate")]
    assert client.asked_files == [(".github/CODEOWNERS", "main")], "the base branch file"
    [pull] = client.pulls
    assert pull["head"] == "sama/fix-gate"
    assert pull["base"] == "main"
    assert pull["title"] == "fix: the gate"
    assert pull["draft"] is False
    assert pull["installation_id"] == 77
    body = pull["body"]
    assert body.startswith("## Summary\n\nFix the gate.\n\n## Changes\n")
    assert "- `src/sama/gate/decide.py` (changed): owner review by @owner @acme/security" in body
    assert "- `docs/roadmap.md` (changed)\n" in body, "an empty owners line clears the owners"
    assert "- `tests/test_x.py` (added)" in body
    assert "## Tests\n\nuv run pytest: 10 passed" in body
    assert OWNER_REVIEW_HEADING in body
    assert "in `.github/CODEOWNERS`" in body
    assert "The agent cannot merge it." in body
    assert client.reviewers == [(12, ("owner",), ("security",))]
    assert "Opened pull request #12 in nikhilllmno/Nikhil_claw" in text
    assert "Review requested from @owner, @acme/security" in text
    assert "You cannot merge it." in text


async def test_open_without_owned_paths_asks_no_one(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.files = [{"filename": "README.md", "status": "modified"}]
    text = await open_pr(sama_pr, signer, draft=True)
    assert service.client.reviewers == []
    assert "No changed path has a code owner" in text
    assert service.client.pulls[0]["draft"] is True
    assert "Opened draft pull request #12" in text


async def test_open_without_a_codeowners_file_says_so(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.owners_files = {"docs/CODEOWNERS": "* @docs-owner\n"}
    await open_pr(sama_pr, signer)
    assert [path for path, _ in service.client.asked_files] == [
        ".github/CODEOWNERS",
        "CODEOWNERS",
        "docs/CODEOWNERS",
    ]
    assert service.client.reviewers[0][1] == ("docs-owner",)

    service.client.owners_files = {}
    service.client.reviewers.clear()
    text = await open_pr(sama_pr, signer)
    assert "has no code owners file on main" in text
    assert service.client.reviewers == []
    assert "The base branch has no code owners file." in service.client.pulls[-1]["body"]


async def test_a_rename_out_of_an_owned_path_counts(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.files = [
        {
            "filename": "src/sama/other.py",
            "previous_filename": "src/sama/gate/old.py",
            "status": "renamed",
        }
    ]
    await open_pr(sama_pr, signer)
    body = service.client.pulls[0]["body"]
    assert (
        "- `src/sama/other.py` (renamed from `src/sama/gate/old.py`): owner review by "
        "@owner @acme/security" in body
    )
    assert service.client.reviewers == [(12, ("owner",), ("security",))]


async def test_a_protected_path_is_named_in_the_body(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.files = [{"filename": ".github/workflows/ci.yml", "status": "modified"}]
    await open_pr(sama_pr, signer)
    body = service.client.pulls[0]["body"]
    assert "protected paths of the sama git proxy" in body
    assert "- `.github/workflows/ci.yml`\n" in body


async def test_a_failed_review_request_does_not_fail_the_tool(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.reviewers_error = GitHubError("GitHub answered 422: not a collaborator", 422)
    text = await open_pr(sama_pr, signer)
    assert "The review request failed (GitHub answered 422: not a collaborator)" in text
    assert "ruleset still requires their approval" in text
    assert len(service.client.pulls) == 1


async def test_email_owners_are_named_but_not_requested(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.owners_files = {"CODEOWNERS": "* owner@example.com\n"}
    text = await open_pr(sama_pr, signer)
    assert service.client.reviewers == []
    assert "code owners owner@example.com. GitHub asks them" in text


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"branch": "main"}, "sama/<name> branch"),
        ({"branch": "feature/x"}, "sama/<name> branch"),
        ({"title": "  "}, "give a title"),
        ({"summary": " "}, "give a summary"),
        ({"tests": ""}, "give the tests that you ran"),
    ],
)
async def test_a_bad_open_is_refused(
    sama_pr: SamaPr,
    service: FakeService,
    signer: RunTokenSigner,
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(SamaPrError, match=message):
        await open_pr(sama_pr, signer, **kwargs)
    assert service.client.pulls == []


async def test_a_branch_that_is_not_pushed_is_refused(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.compare_error = GitHubError("GitHub answered 404: Not Found", 404)
    with pytest.raises(SamaPrError, match="push it first: git push origin sama/fix-gate"):
        await open_pr(sama_pr, signer)
    service.client.compare_error = GitHubError("GitHub answered 500: boom", 500)
    with pytest.raises(SamaPrError, match="did not compare main and sama/fix-gate"):
        await open_pr(sama_pr, signer)
    service.client.compare_error = None
    service.client.ahead_by = 0
    with pytest.raises(SamaPrError, match="has no commits"):
        await open_pr(sama_pr, signer)
    service.client.ahead_by = 1
    service.client.files = []
    with pytest.raises(SamaPrError, match="changes no files"):
        await open_pr(sama_pr, signer)
    assert service.client.pulls == []


async def test_a_change_too_large_to_check_is_refused(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.files = [
        {"filename": f"f{i}.py", "status": "added"} for i in range(MAX_COMPARE_FILES)
    ]
    with pytest.raises(SamaPrError, match="Make a smaller change"):
        await open_pr(sama_pr, signer)


async def test_a_large_codeowners_file_is_refused(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.owners_files = {".github/CODEOWNERS": "#" * (3 * 1024 * 1024 + 1)}
    with pytest.raises(SamaPrError, match=r"did not give \.github/CODEOWNERS of main: .* more"):
        await open_pr(sama_pr, signer)


async def test_a_github_error_on_create_becomes_a_tool_error(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.pull_error = GitHubError("GitHub answered 422: A pull request already exists")
    with pytest.raises(SamaPrError, match="did not open the pull request: GitHub answered 422"):
        await open_pr(sama_pr, signer)


async def test_the_repo_must_be_on_the_allowlist(
    sama_pr: SamaPr, service: FakeService, signer: RunTokenSigner
) -> None:
    with pytest.raises(SamaPrError, match="not on the allowlist"):
        await sama_pr.open(
            claims(signer, uuid.uuid4()),
            branch="sama/x",
            title="T",
            summary="S",
            tests="T",
            draft=False,
        )
    with pytest.raises(SamaPrError, match="not on the allowlist"):
        await sama_pr.start(claims(signer, uuid.uuid4()), name="x", folder=None, token="t")
    other = SamaPr(cast(GitHubService, service), repo="acme/demo")
    with pytest.raises(SamaPrError, match="acme/demo is not on the allowlist"):
        await open_pr(other, signer)


def test_the_repo_defaults_to_sama_and_must_be_valid(service: FakeService) -> None:
    assert SamaPr(cast(GitHubService, service)).repo == DEFAULT_SELF_REPO
    with pytest.raises(ValueError, match="SAMA_SELF_REPO"):
        SamaPr(cast(GitHubService, service), repo="not a repo")


@pytest.mark.parametrize("name", ["", "Fix", "-x", "a/b", "a..b/../c", "x.lock", "x.", "a" * 61])
async def test_a_bad_branch_name_is_refused(
    sama_pr: SamaPr, signer: RunTokenSigner, name: str
) -> None:
    with pytest.raises(SamaPrError):
        await sama_pr.start(claims(signer), name=name, folder=None, token="t")


async def test_start_without_a_sandbox_is_refused(sama_pr: SamaPr, signer: RunTokenSigner) -> None:
    with pytest.raises(SamaPrError, match="no sandbox"):
        await sama_pr.start(claims(signer), name="fix", folder=None, token="t")


def test_the_body_lists_at_most_100_files() -> None:
    files = tuple(ChangedFile(f"f{i}.py", "added") for i in range(MAX_LISTED_FILES + 5))
    review = owner_review(files, None, ())
    body = pull_body(
        summary="S", tests="T", files=files, review=review, codeowners=None, session_id="s1"
    )
    assert body.count("(added)") == MAX_LISTED_FILES
    assert "- and 5 more files" in body
    assert "session `s1`" in body


def test_the_body_stays_below_the_github_limit() -> None:
    """299 renamed, owned, and protected files with long paths, the longest summary, and the
    longest tests."""
    long = "d/" * 80
    files = tuple(
        ChangedFile(f".github/{long}new{i}.py", "renamed", f".github/{long}old{i}.py")
        for i in range(MAX_COMPARE_FILES - 1)
    )
    owners = parse_codeowners("* @owner @acme/security @acme/platform\n")
    review = owner_review(files, owners, (".github/",))
    assert len(review.owned) == 2 * len(files)
    body = pull_body(
        summary="s" * MAX_SUMMARY_CHARS,
        tests="t" * MAX_TESTS_CHARS,
        files=files,
        review=review,
        codeowners=owners,
        session_id=str(uuid.uuid4()),
    )
    assert len(body) < MAX_BODY_CHARS
    assert "more files" in body
    assert body.count("more paths") == 2
    assert body.endswith("The agent cannot merge it.")


def test_owner_review_collects_each_owner_once() -> None:
    owners = parse_codeowners("/a/ @x @y\n/b/ @y @org/t\n")
    files = (ChangedFile("a/1", "added"), ChangedFile("b/2", "added"), ChangedFile("c", "added"))
    review = owner_review(files, owners, ("c",))
    assert review.owners == ("@x", "@y", "@org/t")
    assert review.protected == ("c",)


# -- start, end to end through the git proxy ------------------------------------------------


@dataclass
class World:
    sandbox: Sandbox
    sama_pr: SamaPr
    claims: RunTokenClaims
    token: str
    bare: Path
    store: MemoryStore
    access: Access


class Access:
    """The allowlist that ``RepoCloner`` reads. ``proxy`` off makes a clone without the
    proxy, as for a repository that is not on the allowlist."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.proxy = True

    async def allowed(self, org_id: str, owner: str, name: str) -> bool:
        found = await self.store.allowed(uuid.UUID(org_id), owner, name)
        return self.proxy and found is not None


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    (seed / "app.py").write_text("print(1)\n")
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "first")
    root = tmp_path / "github"
    (root / "nikhilllmno").mkdir(parents=True)
    git(root / "nikhilllmno", "clone", "-q", "--bare", str(seed), "Nikhil_claw.git")
    bare = root / "nikhilllmno" / "Nikhil_claw.git"
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        git(bare, "config", key, "true")

    signer = RunTokenSigner(secrets.token_bytes(32))
    run = scope(uuid.uuid4())
    token = signer.issue(run, ttl_s=600)
    run_claims = signer.verify(token)
    store = MemoryStore()
    store.add(AllowedRepo(uuid.uuid4(), uuid.UUID(run.org_id), 7, "nikhilllmno", "Nikhil_claw"))

    (tmp_path / "sandboxes").mkdir()
    sandbox = await LocalProvider(tmp_path / "sandboxes").create(
        SandboxSpec(org_id=run.org_id, session_id=run.session_id, template="t")
    )

    async def session_sandbox(org_id: str, session_id: str) -> Sandbox | None:
        if (org_id, session_id) == (run.org_id, run.session_id):
            return sandbox
        return None

    access = Access(store)
    proxy = GitProxy(signer, store, LocalUpstream(root))
    with serve(proxy.app()) as url:
        cloner = RepoCloner(
            session_sandbox, source=f"file://{root}", proxy=f"{url}/", access=access
        )
        service = FakeService(store=store)
        tool = SamaPr(cast(GitHubService, service), cloner=cloner, session_sandbox=session_sandbox)
        yield World(sandbox, tool, run_claims, token, bare, store, access)
    await sandbox.kill()


async def test_start_clones_sama_and_the_branch_pushes_through_the_proxy(world: World) -> None:
    text = await world.sama_pr.start(world.claims, name="fix-print", folder=None, token=world.token)
    assert "sama_pr made the branch sama/fix-print" in text
    assert "git push origin sama/fix-print" in text
    assert "Use sama_pr, not pr_create" in text
    assert "You cannot merge it." in text

    found = re.search(r"made the branch sama/fix-print in (\S+)\. Work", text)
    assert found is not None
    folder = found.group(1)
    assert folder.endswith("Nikhil_claw")
    branch = await world.sandbox.exec(["git", "branch", "--show-current"], cwd=folder)
    assert branch.stdout.strip() == "sama/fix-print"
    config = await world.sandbox.exec(["cat", ".git/config"], cwd=folder)
    assert world.token not in config.stdout, "the run token is not written to the clone"

    edit = await world.sandbox.exec(["sh", "-c", "echo 'print(2)' > app.py"], cwd=folder)
    assert edit.exit_code == 0, edit.stderr
    who = ["-c", "user.name=sama", "-c", "user.email=sama@example.test"]
    committed = await world.sandbox.exec(
        ["git", *who, "commit", "-q", "-am", "fix: print 2"], cwd=folder
    )
    assert committed.exit_code == 0, committed.stderr
    env = {"GIT_TERMINAL_PROMPT": "0", "SAMA_RUN_TOKEN": world.token}
    pushed = await world.sandbox.exec(
        ["git", "push", "--quiet", "origin", "sama/fix-print"], cwd=folder, env=env
    )
    assert pushed.exit_code == 0, pushed.stderr
    head = git(world.bare, "rev-parse", "refs/heads/sama/fix-print")
    local = await world.sandbox.exec(["git", "rev-parse", "HEAD"], cwd=folder)
    assert head == local.stdout.strip()

    to_main = await world.sandbox.exec(
        ["git", "push", "--quiet", "origin", "HEAD:main"], cwd=folder, env=env
    )
    assert to_main.exit_code != 0, "the proxy refuses a push to the default branch"


async def test_start_twice_with_the_same_folder_fails(world: World) -> None:
    await world.sama_pr.start(world.claims, name="one", folder="work", token=world.token)
    with pytest.raises(RepoError, match="work"):
        await world.sama_pr.start(world.claims, name="two", folder="work", token=world.token)


async def test_start_refuses_a_clone_without_the_proxy(world: World) -> None:
    world.access.proxy = False
    with pytest.raises(SamaPrError, match="did not use the sama git proxy"):
        await world.sama_pr.start(world.claims, name="x", folder=None, token=world.token)
    world.store.repos.clear()
    with pytest.raises(SamaPrError, match="not on the allowlist"):
        await world.sama_pr.start(world.claims, name="x", folder="other", token=world.token)


# -- the gate and the tool server -------------------------------------------------------------


def test_sama_pr_is_in_the_self_toolset_and_asks() -> None:
    assert "sama_pr" in TOOLSETS["self"]
    assert "sama_pr" not in DEFAULT_ALLOW
    assert "sama_pr" not in ALWAYS_ASK
    assert "sama_pr" not in NEEDS_A_HUMAN
    assert "sama_pr" in BLOCKED_TOOLS, "execute_code cannot call it"
    context = GateContext(toolsets=frozenset({"self"}))
    verdict = decide(context, "mcp__sama__sama_pr", {"action": "start", "name": "x"})
    assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY)
    denied = decide(GateContext(toolsets=frozenset({"core", "github"})), "mcp__sama__sama_pr", {})
    assert denied.action == "deny"
    allow = (PolicyRule(level=RuleLevel.ORG, tool="sama_pr", action="allow"),)
    allowed = decide(
        GateContext(toolsets=frozenset({"self"}), rules=allow), "mcp__sama__sama_pr", {}
    )
    assert allowed.action == "allow"


@pytest.fixture
def tool_url(signer: RunTokenSigner, sama_pr: SamaPr) -> Iterator[str]:
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), sama_pr=sama_pr)
    with serve(server.app()) as base:
        yield base + "/mcp"


async def test_the_tool_is_on_the_server(
    tool_url: str, signer: RunTokenSigner, service: FakeService
) -> None:
    token = signer.issue(scope(), ttl_s=600)
    async with connect(tool_url, token) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "sama_pr" in names
        done = await client.call_tool(
            "sama_pr",
            {
                "action": "open",
                "branch": "sama/fix-gate",
                "title": "T",
                "summary": "S",
                "tests": "pytest: ok",
            },
        )
        no_name = await client.call_tool("sama_pr", {"action": "start"})
        no_branch = await client.call_tool("sama_pr", {"action": "open", "title": "T"})
        no_sandbox = await client.call_tool("sama_pr", {"action": "start", "name": "x"})
    assert not done.is_error, text_of(done)
    assert "Opened pull request #12" in text_of(done)
    assert service.client.pulls[0]["draft"] is False
    assert no_name.is_error
    assert "give name" in text_of(no_name)
    assert no_branch.is_error
    assert "give branch and title" in text_of(no_branch)
    assert no_sandbox.is_error
    assert "no sandbox" in text_of(no_sandbox)


async def test_without_the_app_the_tool_is_hidden(signer: RunTokenSigner) -> None:
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(scope(), ttl_s=600)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert "sama_pr" not in names
