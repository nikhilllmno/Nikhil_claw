"""The pull request tools of the ``github`` toolset (docs/architecture.md, Section 15.1).

- ``pr_create`` opens a pull request from a ``sama/*`` branch.
- ``pr_comment`` adds a comment to a pull request.
- ``repo_push_approval`` records the approval of a push that changes a protected path. The
  gate asks a person for each call, so the body runs only after a person approves.

The tools run on the control plane with the installation token of the repository. The token
never goes to the sandbox. Only repositories on the allowlist of the org can be used.
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from sama.github.client import GitHubError
from sama.github.service import (
    AllowedRepo,
    GitHubService,
    GitHubSetupError,
    split_repo,
    valid_sama_branch,
)
from sama.tenancy.run_token import RunTokenClaims

PR_CREATE: Final = "pr_create"
PR_COMMENT: Final = "pr_comment"
REPO_PUSH_APPROVAL: Final = "repo_push_approval"
MAX_TITLE_CHARS: Final = 256
MAX_BODY_CHARS: Final = 60_000

_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class GitHubToolError(Exception):
    """A GitHub tool failed for a reason that the agent can act on. No secret in it."""


class GitHubTools:
    def __init__(self, service: GitHubService) -> None:
        self._service = service

    async def allowed(self, org_id: str, owner: str, name: str) -> bool:
        """For ``repo_clone``: True if the repository is on the allowlist of the org."""
        try:
            org = uuid.UUID(org_id)
        except ValueError:
            return False
        return await self._service.allowed(org, owner, name) is not None

    async def _repo(self, claims: RunTokenClaims, repo: str) -> AllowedRepo:
        try:
            owner, name = split_repo(repo)
        except GitHubSetupError as error:
            raise GitHubToolError(str(error)) from None
        found = await self._service.allowed(uuid.UUID(claims.org_id), owner, name)
        if found is None:
            raise GitHubToolError(
                f"{owner}/{name} is not on the allowlist of this org. An org admin can add it"
            )
        return found

    async def create_pull(
        self,
        claims: RunTokenClaims,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool,
    ) -> str:
        found = await self._repo(claims, repo)
        if not valid_sama_branch(head):
            raise GitHubToolError("head must be a branch named sama/<name> that you pushed")
        if not _valid_branch(base):
            raise GitHubToolError(f"{base!r} is not a valid branch name")
        if not title.strip():
            raise GitHubToolError("give a title")
        try:
            pull = await self._service.client.create_pull(
                found.installation_id,
                found.owner,
                found.name,
                head=head,
                base=base,
                title=title.strip()[:MAX_TITLE_CHARS],
                body=body[:MAX_BODY_CHARS],
                draft=draft,
            )
        except GitHubError as error:
            raise GitHubToolError(f"GitHub did not open the pull request: {error}") from None
        kind = "draft pull request" if pull.get("draft") else "pull request"
        return (
            f"Opened {kind} #{pull.get('number')} in {found.full_name}: "
            f"{pull.get('html_url')}. It merges {head} into {base}. A person reviews and merges "
            "it. You cannot merge it."
        )

    async def comment(self, claims: RunTokenClaims, repo: str, number: int, body: str) -> str:
        found = await self._repo(claims, repo)
        if not body.strip():
            raise GitHubToolError("give the text of the comment")
        try:
            # The pull request endpoint answers 404 for an issue, so the tool comments only
            # on pull requests.
            await self._service.client.pull(found.installation_id, found.owner, found.name, number)
            made = await self._service.client.create_comment(
                found.installation_id, found.owner, found.name, number, body[:MAX_BODY_CHARS]
            )
        except GitHubError as error:
            if error.status_code == 404:
                raise GitHubToolError(f"{found.full_name} has no pull request #{number}") from None
            raise GitHubToolError(f"GitHub did not add the comment: {error}") from None
        return f"Commented on pull request #{number} of {found.full_name}: {made.get('html_url')}"

    async def approve_push(
        self, claims: RunTokenClaims, repo: str, branch: str, commit: str
    ) -> str:
        found = await self._repo(claims, repo)
        if not valid_sama_branch(branch):
            raise GitHubToolError("branch must be a branch named sama/<name>")
        if not _SHA.fullmatch(commit):
            raise GitHubToolError("commit must be the full 40-character commit id")
        expires = await self._service.grant_push(
            found,
            f"refs/heads/{branch}",
            commit,
            session_id=uuid.UUID(claims.session_id),
            run_id=uuid.UUID(claims.run_id),
            user_id=uuid.UUID(claims.user_id) if claims.user_id else None,
        )
        return (
            f"A person approved the push of commit {commit} to {branch} of {found.full_name}. "
            f"Push it now: the approval works once, until {expires.isoformat(timespec='minutes')}."
        )


def _valid_branch(branch: str) -> bool:
    return (
        bool(_BRANCH.fullmatch(branch))
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
    )
