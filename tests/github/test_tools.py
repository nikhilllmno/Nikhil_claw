"""The pull request tools and their place in the tool server and the gate (Section 15.1)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from sama.gate.decide import (
    ALWAYS_ASK,
    NEEDS_A_HUMAN,
    TOOLSETS,
    GateContext,
    GateStep,
    PolicyRule,
    RuleLevel,
    decide,
)
from sama.github.client import GitHubError
from sama.github.service import AllowedRepo, GitHubService
from sama.runtime.contract import Audience, RunScope
from sama.tenancy import RunTokenClaims, RunTokenSigner
from sama.tools import MemoryTodoStore, ToolServer
from sama.tools.github import GitHubToolError, GitHubTools
from tests.conformance.scripted_model import serve
from tests.tools.test_server import connect, text_of

ORG = uuid.uuid4()
SHA = "c" * 40


@dataclass
class FakeClient:
    pulls: list[dict[str, Any]] = field(default_factory=list)
    comments: list[tuple[int, str]] = field(default_factory=list)
    known_pulls: set[int] = field(default_factory=lambda: {5})
    fail: GitHubError | None = None

    async def create_pull(self, installation_id: int, owner: str, name: str, **kw: Any) -> Any:
        if self.fail is not None:
            raise self.fail
        self.pulls.append({"installation_id": installation_id, "repo": f"{owner}/{name}", **kw})
        return {"number": 9, "html_url": f"https://github.com/{owner}/{name}/pull/9", **kw}

    async def pull(self, installation_id: int, owner: str, name: str, number: int) -> Any:
        if number not in self.known_pulls:
            raise GitHubError("GitHub answered 404: Not Found", 404)
        return {"number": number}

    async def create_comment(
        self, installation_id: int, owner: str, name: str, number: int, body: str
    ) -> Any:
        self.comments.append((number, body))
        return {"html_url": f"https://github.com/{owner}/{name}/pull/{number}#c1"}


@dataclass
class FakeService:
    client: FakeClient = field(default_factory=FakeClient)
    repo: AllowedRepo = field(
        default_factory=lambda: AllowedRepo(uuid.uuid4(), ORG, 77, "Acme", "Demo")
    )
    grants: list[tuple[str, str, uuid.UUID]] = field(default_factory=list)

    async def allowed(self, org_id: uuid.UUID, owner: str, name: str) -> AllowedRepo | None:
        if org_id == ORG and f"{owner}/{name}".lower() == "acme/demo":
            return self.repo
        return None

    async def grant_push(
        self,
        repo: AllowedRepo,
        ref: str,
        commit: str,
        *,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        user_id: uuid.UUID | None,
    ) -> datetime:
        self.grants.append((ref, commit, run_id))
        return datetime(2026, 1, 1, 12, 0, tzinfo=UTC) + timedelta(hours=1)


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
    return FakeService()


@pytest.fixture
def tools(service: FakeService) -> GitHubTools:
    return GitHubTools(cast(GitHubService, service))


def claims(signer: RunTokenSigner, org: uuid.UUID = ORG) -> RunTokenClaims:
    return signer.verify(signer.issue(scope(org), ttl_s=600))


async def test_a_pull_request_opens_from_a_sama_branch(
    tools: GitHubTools, service: FakeService, signer: RunTokenSigner
) -> None:
    text = await tools.create_pull(
        claims(signer),
        "https://github.com/acme/demo",
        head="sama/fix",
        base="main",
        title="  Fix it  ",
        body="Body",
        draft=False,
    )
    assert "Opened pull request #9 in Acme/Demo" in text
    assert "You cannot merge it" in text
    assert service.client.pulls == [
        {
            "installation_id": 77,
            "repo": "Acme/Demo",
            "head": "sama/fix",
            "base": "main",
            "title": "Fix it",
            "body": "Body",
            "draft": False,
        }
    ]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"head": "main"}, "head must be a branch named sama/<name>"),
        ({"head": "feature/x"}, "head must be a branch named sama/<name>"),
        ({"base": "../x"}, "is not a valid branch name"),
        ({"title": "   "}, "give a title"),
    ],
)
async def test_a_bad_pull_request_is_refused(
    tools: GitHubTools, signer: RunTokenSigner, kwargs: dict[str, Any], message: str
) -> None:
    args: dict[str, Any] = {
        "head": "sama/x",
        "base": "main",
        "title": "T",
        "body": "",
        "draft": False,
    }
    args.update(kwargs)
    with pytest.raises(GitHubToolError, match=message):
        await tools.create_pull(claims(signer), "acme/demo", **args)


async def test_a_repository_off_the_allowlist_is_refused(
    tools: GitHubTools, signer: RunTokenSigner
) -> None:
    with pytest.raises(GitHubToolError, match="not on the allowlist"):
        await tools.create_pull(
            claims(signer),
            "acme/other",
            head="sama/x",
            base="main",
            title="T",
            body="",
            draft=False,
        )
    with pytest.raises(GitHubToolError, match="not on the allowlist"):
        await tools.comment(claims(signer, uuid.uuid4()), "acme/demo", 5, "hi")
    with pytest.raises(GitHubToolError):
        await tools.comment(claims(signer), "not a repo", 5, "hi")
    assert await tools.allowed(str(ORG), "acme", "demo")
    assert not await tools.allowed(str(ORG), "acme", "other")
    assert not await tools.allowed("org-1", "acme", "demo")


async def test_a_github_error_becomes_a_tool_error(
    tools: GitHubTools, service: FakeService, signer: RunTokenSigner
) -> None:
    service.client.fail = GitHubError("GitHub answered 422: No commits between main and sama/x")
    with pytest.raises(GitHubToolError, match="did not open the pull request: GitHub answered 422"):
        await tools.create_pull(
            claims(signer), "acme/demo", head="sama/x", base="main", title="T", body="", draft=False
        )


async def test_a_comment_goes_only_to_a_pull_request(
    tools: GitHubTools, service: FakeService, signer: RunTokenSigner
) -> None:
    text = await tools.comment(claims(signer), "acme/demo", 5, "Looks good")
    assert "Commented on pull request #5 of Acme/Demo" in text
    assert service.client.comments == [(5, "Looks good")]
    with pytest.raises(GitHubToolError, match="has no pull request #6"):
        await tools.comment(claims(signer), "acme/demo", 6, "hi")
    with pytest.raises(GitHubToolError, match="give the text"):
        await tools.comment(claims(signer), "acme/demo", 5, "  ")


async def test_a_push_approval_records_a_grant(
    tools: GitHubTools, service: FakeService, signer: RunTokenSigner
) -> None:
    run = claims(signer)
    text = await tools.approve_push(run, "acme/demo", "sama/fix", SHA)
    assert service.grants == [("refs/heads/sama/fix", SHA, uuid.UUID(run.run_id))]
    assert "approval works once" in text
    with pytest.raises(GitHubToolError, match="sama/<name>"):
        await tools.approve_push(run, "acme/demo", "main", SHA)
    with pytest.raises(GitHubToolError, match="40-character"):
        await tools.approve_push(run, "acme/demo", "sama/fix", "abc123")


# -- the gate ---------------------------------------------------------------------------------


def test_the_github_toolset_has_the_new_tools() -> None:
    assert {"repo_clone", "pr_create", "pr_comment", "repo_push_approval"} <= TOOLSETS["github"]
    context = GateContext(toolsets=frozenset({"github"}))
    for tool in ("pr_create", "pr_comment"):
        verdict = decide(context, f"mcp__sama__{tool}", {})
        assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY), "a write to GitHub"
    denied = decide(GateContext(toolsets=frozenset({"core"})), "mcp__sama__pr_create", {})
    assert denied.action == "deny"


def test_a_push_approval_always_asks_a_person() -> None:
    assert "repo_push_approval" in ALWAYS_ASK
    assert "repo_push_approval" in NEEDS_A_HUMAN
    allow_all = (PolicyRule(level=RuleLevel.USER, tool="repo_push_approval", action="allow"),)
    context = GateContext(toolsets=frozenset({"github"}), rules=allow_all)
    verdict = decide(context, "mcp__sama__repo_push_approval", {})
    assert (verdict.action, verdict.step) == ("ask", GateStep.POLICY)
    deny = (PolicyRule(level=RuleLevel.ORG, tool="repo_push_approval", action="deny"),)
    denied = decide(
        GateContext(toolsets=frozenset({"github"}), rules=deny), "mcp__sama__repo_push_approval", {}
    )
    assert denied.action == "deny"


# -- the tool server --------------------------------------------------------------------------


@pytest.fixture
def tool_url(signer: RunTokenSigner, tools: GitHubTools) -> Iterator[str]:
    server = ToolServer(signer=signer, todos=MemoryTodoStore(), github=tools)
    with serve(server.app()) as base:
        yield base + "/mcp"


async def test_the_tools_are_on_the_server(tool_url: str, signer: RunTokenSigner) -> None:
    token = signer.issue(scope(), ttl_s=600)
    async with connect(tool_url, token) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"pr_create", "pr_comment", "repo_push_approval"} <= names
        done = await client.call_tool(
            "pr_create",
            {"repo": "acme/demo", "head": "sama/x", "base": "main", "title": "T"},
        )
        refused = await client.call_tool(
            "pr_create",
            {"repo": "acme/demo", "head": "main", "base": "main", "title": "T"},
        )
    assert not done.is_error
    assert "Opened pull request #9" in text_of(done)
    assert refused.is_error
    assert "sama/<name>" in text_of(refused)


async def test_without_the_app_the_tools_are_hidden(signer: RunTokenSigner) -> None:
    server = ToolServer(signer=signer, todos=MemoryTodoStore())
    with serve(server.app()) as base:
        async with connect(base + "/mcp", signer.issue(scope(), ttl_s=600)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
    assert not names & {"pr_create", "pr_comment", "repo_push_approval"}
