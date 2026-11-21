"""``sama_pr``: the agent changes the code of sama itself (docs/architecture.md, Section 15.2).

The tool is in the toolset ``self``. It works only on one repository: the sama repository of
the deployment (``SAMA_SELF_REPO``). The repository must be on the allowlist of the org of the
run, so it uses the sama GitHub App and the sama git proxy of Section 15.1.

- ``start`` clones the sama repository into the sandbox through the git proxy and makes the
  branch ``sama/<name>``. The agent then changes the code, runs the tests, commits, and pushes
  the branch with git in its shell. The git proxy checks the push.
- ``open`` opens a pull request from the pushed branch to the default branch. The body has the
  summary of the agent, the changed files, and the tests that the agent ran. The tool reads the
  code owners file of the default branch. If a changed path has owners, the tool asks them for
  a review. The ruleset of the repository requires their approval (Section 19.5).

The agent cannot merge. Sama has no merge tool, and no GitHub token goes to the sandbox.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sama.github.client import GitHubError
from sama.github.codeowners import (
    CODEOWNERS_PATHS,
    MAX_CODEOWNERS_BYTES,
    CodeOwners,
    parse_codeowners,
    split_owners,
)
from sama.github.service import (
    DEFAULT_SELF_REPO,
    AllowedRepo,
    GitHubService,
    GitHubSetupError,
    is_protected,
    split_repo,
    valid_sama_branch,
)
from sama.sandbox.base import SandboxError
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.output import SessionSandbox
from sama.tools.repos import GIT_TIMEOUT_S, RepoCloner
from sama.tools.repos import render as render_clone

SAMA_PR: Final = "sama_pr"
MAX_TITLE_CHARS: Final = 256
MAX_SUMMARY_CHARS: Final = 10_000
MAX_TESTS_CHARS: Final = 10_000
MAX_NAME_CHARS: Final = 60
MAX_LISTED_FILES: Final = 100
"""The body lists at most this many changed files."""
MAX_COMPARE_FILES: Final = 300
"""GitHub gives at most 300 files in a comparison. With 300 files, the tool cannot see every
changed path, so it refuses the pull request."""
OWNER_REVIEW_HEADING: Final = "## Owner review"

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_STATUS_WORDS: Final = {
    "added": "added",
    "removed": "removed",
    "modified": "changed",
    "renamed": "renamed",
    "copied": "copied",
    "changed": "changed",
    "unchanged": "unchanged",
}


class SamaPrError(Exception):
    """``sama_pr`` failed for a reason that the agent can act on. No secret in it."""


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: str
    previous: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        """The path, and the old path of a rename. A rename out of an owned path counts."""
        return (self.path,) if self.previous is None else (self.path, self.previous)


@dataclass(frozen=True, slots=True)
class OwnerReview:
    """The changed paths that have code owners, and the owners to ask."""

    owned: tuple[tuple[str, tuple[str, ...]], ...]
    """``(path, owners)`` for each changed path with owners."""
    protected: tuple[str, ...]
    """The changed paths that are protected paths of the git proxy (Section 15.1)."""

    @property
    def owners(self) -> tuple[str, ...]:
        found: dict[str, None] = {}
        for _, owners in self.owned:
            for owner in owners:
                found[owner] = None
        return tuple(found)


def owner_review(
    files: tuple[ChangedFile, ...], codeowners: CodeOwners | None, protected: tuple[str, ...]
) -> OwnerReview:
    owned: list[tuple[str, tuple[str, ...]]] = []
    hits: list[str] = []
    for changed in files:
        for path in changed.paths:
            owners = codeowners.owners_of(path) if codeowners is not None else ()
            if owners:
                owned.append((path, owners))
            if is_protected(path, protected):
                hits.append(path)
    return OwnerReview(tuple(owned), tuple(dict.fromkeys(hits)))


class SamaPr:
    """The body of ``sama_pr``. ``cloner`` and ``session_sandbox`` are needed for ``start``;
    without them, ``start`` tells the agent that the server has no sandbox."""

    def __init__(
        self,
        service: GitHubService,
        *,
        cloner: RepoCloner | None = None,
        session_sandbox: SessionSandbox | None = None,
        repo: str = DEFAULT_SELF_REPO,
    ) -> None:
        self._service = service
        self._cloner = cloner
        self._session_sandbox = session_sandbox
        try:
            self._owner, self._name = split_repo(repo)
        except GitHubSetupError as error:
            raise ValueError(f"SAMA_SELF_REPO: {error}") from None

    @property
    def repo(self) -> str:
        return f"{self._owner}/{self._name}"

    async def _allowed(self, claims: RunTokenClaims) -> AllowedRepo:
        found = await self._service.allowed(uuid.UUID(claims.org_id), self._owner, self._name)
        if found is None:
            raise SamaPrError(
                f"the sama repository {self.repo} is not on the allowlist of this org. An org "
                "admin must install the sama GitHub App on it and add it to the allowlist"
            )
        return found

    async def start(
        self, claims: RunTokenClaims, *, name: str, folder: str | None, token: str | None
    ) -> str:
        """Clone the sama repository and make the branch ``sama/<name>``."""
        if not _NAME.fullmatch(name) or len(name) > MAX_NAME_CHARS or name.endswith((".", ".lock")):
            raise SamaPrError(
                f"name must be a short name of lower-case letters, digits, '.', '_', or '-' "
                f"(at most {MAX_NAME_CHARS} characters), for example fix-retry-limit"
            )
        branch = f"sama/{name}"
        if not valid_sama_branch(branch):
            raise SamaPrError(f"{branch} is not a valid branch name")
        found = await self._allowed(claims)
        if self._cloner is None or self._session_sandbox is None:
            raise SamaPrError("this server has no sandbox, so sama_pr cannot clone")
        cloned = await self._cloner.clone(claims, found.full_name, folder=folder, token=token)
        if not cloned.proxied:
            raise SamaPrError(
                "the clone did not use the sama git proxy, so you cannot push it. Ask an "
                "operator to check the GitHub App setup"
            )
        sandbox = await self._session_sandbox(claims.org_id, claims.session_id)
        if sandbox is None:
            raise SamaPrError("the session has no sandbox")
        try:
            switched = await sandbox.exec(
                ["git", "switch", "--quiet", "--create", branch],
                cwd=cloned.path,
                timeout_s=GIT_TIMEOUT_S,
            )
        except SandboxError as error:
            raise SamaPrError(f"the sandbox failed: {error}") from None
        if switched.exit_code != 0:
            raise SamaPrError(f"git cannot make the branch {branch} in {cloned.path}")
        return "\n\n".join(
            [
                render_clone(cloned),
                (
                    f"sama_pr made the branch {branch} in {cloned.path}. Work in this order:\n"
                    "1. Change the code. Follow AGENTS.md: update the docs in the same change.\n"
                    "2. Run the checks and the tests that AGENTS.md names (for sama: uv run "
                    "ruff check ., uv run ruff format --check ., uv run mypy, uv run pytest). "
                    "Fix each failure.\n"
                    "3. Commit with a Conventional Commit message.\n"
                    f"4. Push: git push origin {branch}.\n"
                    f"5. Call sama_pr with action open, branch {branch}, a title, a summary of "
                    "the change, and the tests that you ran with their results. Use sama_pr, "
                    "not pr_create, for this repository.\n"
                    "A person reviews and merges the pull request. You cannot merge it."
                ),
            ]
        )

    async def open(
        self,
        claims: RunTokenClaims,
        *,
        branch: str,
        title: str,
        summary: str,
        tests: str,
        draft: bool,
    ) -> str:
        """Open the pull request and ask the code owners of the changed paths for a review."""
        if not valid_sama_branch(branch):
            raise SamaPrError("branch must be the sama/<name> branch that you pushed")
        if not title.strip():
            raise SamaPrError("give a title")
        if not summary.strip():
            raise SamaPrError("give a summary of the change")
        if not tests.strip():
            raise SamaPrError(
                "give the tests that you ran and their results. Run the tests before you open "
                "the pull request"
            )
        found = await self._allowed(claims)
        client = self._service.client
        installation, owner, name = found.installation_id, found.owner, found.name
        try:
            base = str((await client.repo(installation, owner, name)).get("default_branch") or "")
        except GitHubError as error:
            raise SamaPrError(f"GitHub did not give the repository: {error}") from None
        if not base:
            raise SamaPrError(f"GitHub gave no default branch for {found.full_name}")
        files = await self._changed_files(found, base, branch)
        codeowners = await self._codeowners(found, base)
        review = owner_review(files, codeowners, found.protected_paths)
        body = pull_body(
            summary=summary,
            tests=tests,
            files=files,
            review=review,
            codeowners=codeowners,
            session_id=claims.session_id,
        )
        try:
            pull = await client.create_pull(
                installation,
                owner,
                name,
                head=branch,
                base=base,
                title=title.strip()[:MAX_TITLE_CHARS],
                body=body,
                draft=draft,
            )
        except GitHubError as error:
            raise SamaPrError(f"GitHub did not open the pull request: {error}") from None
        number = pull.get("number")
        kind = "draft pull request" if pull.get("draft") else "pull request"
        lines = [
            f"Opened {kind} #{number} in {found.full_name}: {pull.get('html_url')}. It merges "
            f"{branch} into {base}. It changes {len(files)} files."
        ]
        lines.append(await self._request_review(found, number, review))
        if codeowners is None:
            lines.append(
                f"{found.full_name} has no code owners file on {base}, so no owner review was "
                "requested."
            )
        lines.append("A person reviews and merges it. You cannot merge it.")
        return "\n".join(lines)

    async def _changed_files(
        self, found: AllowedRepo, base: str, branch: str
    ) -> tuple[ChangedFile, ...]:
        client = self._service.client
        try:
            comparison = await client.compare(
                found.installation_id, found.owner, found.name, base, branch
            )
        except GitHubError as error:
            if error.status_code == 404:
                raise SamaPrError(
                    f"GitHub has no branch {branch} in {found.full_name}. Commit, then push it "
                    f"first: git push origin {branch}"
                ) from None
            raise SamaPrError(f"GitHub did not compare {base} and {branch}: {error}") from None
        ahead = comparison.get("ahead_by")
        if not isinstance(ahead, int) or ahead < 1:
            raise SamaPrError(
                f"{branch} has no commits that {base} does not have. Commit and push your change "
                "first"
            )
        raw = comparison.get("files")
        entries: list[Any] = raw if isinstance(raw, list) else []
        if len(entries) >= MAX_COMPARE_FILES:
            raise SamaPrError(
                f"the change has {MAX_COMPARE_FILES} changed files or more, so sama_pr cannot "
                "check each path. Make a smaller change"
            )
        files: list[ChangedFile] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
                continue
            previous = entry.get("previous_filename")
            files.append(
                ChangedFile(
                    path=entry["filename"],
                    status=str(entry.get("status") or "changed"),
                    previous=previous if isinstance(previous, str) else None,
                )
            )
        if not files:
            raise SamaPrError(f"{branch} changes no files compared with {base}")
        return tuple(files)

    async def _codeowners(self, found: AllowedRepo, base: str) -> CodeOwners | None:
        """The code owners file of the base branch. GitHub uses this file for the review rule,
        so a change to the file on the branch does not change the owners of this pull
        request."""
        client = self._service.client
        for path in CODEOWNERS_PATHS:
            try:
                text = await client.file_text(
                    found.installation_id, found.owner, found.name, path, base
                )
            except GitHubError as error:
                raise SamaPrError(f"GitHub did not give {path} of {base}: {error}") from None
            if text is None:
                continue
            if len(text.encode("utf-8")) > MAX_CODEOWNERS_BYTES:
                raise SamaPrError(f"{path} is larger than 3 MB, and GitHub ignores it")
            return parse_codeowners(text, path=path)
        return None

    async def _request_review(self, found: AllowedRepo, number: Any, review: OwnerReview) -> str:
        if not review.owned:
            return "No changed path has a code owner, so no owner review was requested."
        users, teams = split_owners(review.owners)
        named = ", ".join(review.owners)
        if not isinstance(number, int) or not (users or teams):
            return f"Changed paths have the code owners {named}. GitHub asks them for a review."
        try:
            await self._service.client.request_reviewers(
                found.installation_id, found.owner, found.name, number, users=users, teams=teams
            )
        except GitHubError as error:
            # GitHub also asks the code owners itself, and the ruleset requires their approval.
            return (
                f"Changed paths have the code owners {named}. The review request failed "
                f"({error}), but the ruleset still requires their approval."
            )
        return f"Changed paths have code owners. Review requested from {named}."


def pull_body(
    *,
    summary: str,
    tests: str,
    files: tuple[ChangedFile, ...],
    review: OwnerReview,
    codeowners: CodeOwners | None,
    session_id: str,
) -> str:
    """The body of the pull request, in Markdown."""
    owned = dict(review.owned)
    lines = ["## Summary", "", summary.strip()[:MAX_SUMMARY_CHARS], "", "## Changes", ""]
    for changed in files[:MAX_LISTED_FILES]:
        word = _STATUS_WORDS.get(changed.status, changed.status)
        line = f"- `{changed.path}` ({word}"
        if changed.previous:
            line += f" from `{changed.previous}`"
        line += ")"
        owners = owned.get(changed.path) or (owned.get(changed.previous or "") or ())
        if owners:
            line += f": owner review by {' '.join(owners)}"
        lines.append(line)
    if len(files) > MAX_LISTED_FILES:
        lines.append(f"- and {len(files) - MAX_LISTED_FILES} more files")
    lines += ["", "## Tests", "", tests.strip()[:MAX_TESTS_CHARS], "", OWNER_REVIEW_HEADING, ""]
    if codeowners is None:
        lines.append("The base branch has no code owners file.")
    elif review.owned:
        lines.append(f"These changed paths have code owners in `{codeowners.path}`:")
        lines.append("")
        lines += [f"- `{path}`: {' '.join(owners)}" for path, owners in review.owned]
    else:
        lines.append(f"No changed path has a code owner in `{codeowners.path}`.")
    if review.protected:
        lines += [
            "",
            "These changed paths are protected paths of the sama git proxy. A person approved "
            "the push:",
            "",
        ]
        lines += [f"- `{path}`" for path in review.protected]
    lines += [
        "",
        "---",
        f"Opened by the sama agent with `sama_pr` (session `{session_id}`). A person reviews and "
        "merges this pull request. The agent cannot merge it.",
    ]
    return "\n".join(lines)
