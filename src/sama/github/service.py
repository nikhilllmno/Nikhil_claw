"""The GitHub connections of the orgs (docs/architecture.md, Section 15.1).

- An org admin installs the sama GitHub App. The install URL has a signed state that binds
  the org and the admin. GitHub sends the admin back to ``/github/setup`` with the
  installation id and an OAuth code. Sama checks that the admin can see the installation
  with that code, because the installation id in the URL alone proves nothing.
- An org admin puts repositories on the allowlist of the org. Only these repositories go
  through the git proxy and get pull requests.
- A person approves a ``repo_push_approval`` call. The approval is a push grant: the git
  proxy uses it once to let one commit that changes a protected path go to one branch.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sama.db.models import AuditEntry, GitHubInstallation, GitHubPushGrant, GitHubRepo, Org
from sama.github.client import GitHubClient, GitHubError
from sama.tenancy.access import load_grants
from sama.tenancy.authority import load_authority

log = logging.getLogger("sama.github")

DEFAULT_PROTECTED_PATHS: Final = (
    ".github/workflows/",
    ".github/actions/",
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)
"""A push that changes one of these paths needs an approval. GitHub runs the workflows of the
pushed branch, and the code owners file decides who must review a pull request."""
MAX_PROTECTED_PATHS: Final = 50
MAX_PATH_CHARS: Final = 200
STATE_TTL_S: Final = 30 * 60
"""The install URL works for 30 minutes."""
PUSH_GRANT_TTL: Final = timedelta(hours=1)
SAMA_BRANCH_PREFIX: Final = "sama/"
SAMA_REF_PREFIX: Final = f"refs/heads/{SAMA_BRANCH_PREFIX}"

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}")
_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")


class GitHubSetupError(Exception):
    """A setup step failed for a reason that the caller can act on. No secret in it."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def split_repo(text: str) -> tuple[str, str]:
    """``(owner, name)`` of ``owner/name``, ``github.com/owner/name``, or a GitHub URL."""
    raw = text.strip().removesuffix("/").removesuffix(".git")
    for prefix in ("https://github.com/", "http://github.com/", "github.com/"):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix) :]
            break
    owner, _, name = raw.partition("/")
    if not _OWNER.fullmatch(owner) or not _NAME.fullmatch(name) or name in {".", ".."}:
        raise GitHubSetupError("give the repository as <owner>/<name>")
    return owner, name


def valid_sama_branch(branch: str) -> bool:
    """True for a branch name ``sama/<name>`` that git accepts."""
    return (
        branch.startswith(SAMA_BRANCH_PREFIX)
        and len(branch) > len(SAMA_BRANCH_PREFIX)
        and bool(_BRANCH.fullmatch(branch))
        and ".." not in branch
        and "//" not in branch
        and not branch.endswith(("/", ".", ".lock"))
        and "/." not in branch
    )


def normalize_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """Checks the extra protected paths of a repository. Raises ``GitHubSetupError``."""
    found: list[str] = []
    for raw in paths:
        path = raw.strip().removeprefix("./")
        if (
            not path
            or len(path) > MAX_PATH_CHARS
            or path.startswith("/")
            or ".." in path.split("/")
            or "\\" in path
            or any(ord(char) < 32 for char in path)
        ):
            raise GitHubSetupError(f"{raw!r} is not a valid path in a repository")
        if path not in found:
            found.append(path)
    if len(found) > MAX_PROTECTED_PATHS:
        raise GitHubSetupError(f"give at most {MAX_PROTECTED_PATHS} protected paths")
    return tuple(found)


def is_protected(path: str, protected: Iterable[str]) -> bool:
    """True if ``path`` is a protected path or is in a protected folder. The check ignores
    case, so ``.GitHub/Workflows/x.yml`` also counts."""
    lowered = path.lower()
    for entry in protected:
        rule = entry.lower()
        if rule.endswith("/"):
            if lowered.startswith(rule):
                return True
        elif lowered == rule or lowered.startswith(rule + "/"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class AllowedRepo:
    """A repository on the allowlist of an org."""

    id: uuid.UUID
    org_id: uuid.UUID
    installation_id: int
    """The id of the installation on GitHub."""
    owner: str
    name: str
    extra_paths: tuple[str, ...] = ()

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def protected_paths(self) -> tuple[str, ...]:
        return (*DEFAULT_PROTECTED_PATHS, *self.extra_paths)

    def public(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "repo": self.full_name,
            "installation_id": self.installation_id,
            "protected_paths": list(self.protected_paths),
        }


@dataclass(frozen=True, slots=True)
class InstallState:
    org_id: uuid.UUID
    user_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class InstallPreview:
    org_name: str
    account: str
    account_type: str


class GitHubService:
    """The installations, the allowlist, and the push grants of the orgs."""

    def __init__(
        self,
        engine: AsyncEngine,
        client: GitHubClient,
        *,
        state_key: bytes,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = engine
        self.client = client
        self._state_key = hmac.new(state_key, b"sama github install state", "sha256").digest()
        self._clock = clock

    # -- install ----------------------------------------------------------------------------

    def install_url(self, org_id: uuid.UUID, user_id: uuid.UUID) -> str:
        payload = {"o": str(org_id), "u": str(user_id), "e": int(self._clock()) + STATE_TTL_S}
        raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
        state = f"{raw}.{_b64(self._sign(raw))}"
        return self.client.config.install_url(state)

    def _sign(self, raw: str) -> bytes:
        return hmac.new(self._state_key, raw.encode(), "sha256").digest()

    def read_state(self, state: str) -> InstallState:
        raw, _, signature = state.partition(".")
        try:
            given = _unb64(signature)
            payload = json.loads(_unb64(raw))
            if not hmac.compare_digest(given, self._sign(raw)):
                raise ValueError("signature")
            if int(payload["e"]) < self._clock():
                raise GitHubSetupError("the install link expired. Start the install again")
            return InstallState(uuid.UUID(payload["o"]), uuid.UUID(payload["u"]))
        except (ValueError, KeyError, TypeError):
            raise GitHubSetupError("the install link is not valid") from None

    async def preview_install(self, state: str, installation_id: int) -> InstallPreview:
        """What a confirmed install connects: the sama org and the GitHub account. The page
        shows both, so the person who installed the App sees where the access goes."""
        who = self.read_state(state)
        login, kind = await self._account(installation_id)
        async with self._engine.connect() as connection:
            org_name = await self._org_of_admin(connection, who)
        return InstallPreview(org_name, login, kind)

    async def _account(self, installation_id: int) -> tuple[str, str]:
        try:
            found = await self.client.installation(installation_id)
        except GitHubError as error:
            raise GitHubSetupError(str(error), 404 if error.status_code == 404 else 502) from None
        account = found.get("account") or {}
        login = str(account.get("login") or "")
        kind = str(account.get("type") or "User")
        if not login:
            raise GitHubSetupError("GitHub gave no account for the installation", 502)
        return login, kind

    async def _org_of_admin(self, connection: AsyncConnection, who: InstallState) -> str:
        """The name of the org. The person who made the install link must still be an org
        admin: a link stops working when that person loses the role."""
        grants = await load_grants(connection, who.org_id, who.user_id)
        name = await connection.scalar(select(Org.name).where(Org.id == who.org_id))
        if name is None or not grants.org_admin:
            raise GitHubSetupError(
                "the person who made the install link is not an org admin now", 403
            )
        return str(name)

    async def finish_install(self, state: str, installation_id: int, code: str) -> dict[str, Any]:
        """Links the installation to the org of the state, after the OAuth check."""
        who = self.read_state(state)
        login, kind = await self._account(installation_id)
        organization = kind == "Organization"
        try:
            access = await self.client.user_access(code, org=login if organization else None)
        except GitHubError as error:
            raise GitHubSetupError(str(error), 400) from None
        if installation_id not in access.installation_ids:
            # The id in the URL is not proof: anyone can type an id. The person who installs
            # must see the installation with their own GitHub sign-in.
            raise GitHubSetupError("your GitHub account cannot see this installation", 403)
        # To see an installation is not enough: a collaborator with read access to one
        # repository sees it too. Only the owner of the GitHub account can connect it.
        if organization and not access.org_admin:
            raise GitHubSetupError(
                f"only an owner of the GitHub organization {login} can connect it", 403
            )
        if not organization and access.login.lower() != login.lower():
            raise GitHubSetupError(f"only the GitHub user {login} can connect this account", 403)
        async with self._engine.begin() as connection:
            await self._org_of_admin(connection, who)
            owner = await connection.scalar(
                select(GitHubInstallation.org_id).where(
                    GitHubInstallation.installation_id == installation_id
                )
            )
            if owner is not None and owner != who.org_id:
                raise GitHubSetupError("another sama org already uses this installation", 409)
            if owner is None:
                row_id = await connection.scalar(
                    insert(GitHubInstallation)
                    .values(
                        org_id=who.org_id,
                        installation_id=installation_id,
                        account_login=login,
                        account_type=kind,
                        created_by_user_id=who.user_id,
                    )
                    .returning(GitHubInstallation.id)
                )
            else:
                row_id = await connection.scalar(
                    update(GitHubInstallation)
                    .where(GitHubInstallation.installation_id == installation_id)
                    .values(account_login=login, account_type=kind, updated_at=_now())
                    .returning(GitHubInstallation.id)
                )
            await _audit(
                connection,
                who.org_id,
                who.user_id,
                "github.installed",
                "github_installation",
                str(row_id),
                {"installation_id": installation_id, "account": login},
            )
        return {
            "org_id": str(who.org_id),
            "installation_id": installation_id,
            "account": login,
            "account_type": kind,
        }

    async def installations(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                select(
                    GitHubInstallation.id,
                    GitHubInstallation.installation_id,
                    GitHubInstallation.account_login,
                    GitHubInstallation.account_type,
                    GitHubInstallation.created_at,
                )
                .where(GitHubInstallation.org_id == org_id)
                .order_by(GitHubInstallation.created_at)
            )
            return [
                {
                    "id": str(row.id),
                    "installation_id": row.installation_id,
                    "account": row.account_login,
                    "account_type": row.account_type,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def remove_installation(
        self, org_id: uuid.UUID, user_id: uuid.UUID, row_id: uuid.UUID
    ) -> bool:
        """Forgets the installation and its repositories. The App stays installed on GitHub
        until an owner of the GitHub account removes it there."""
        async with self._engine.begin() as connection:
            gone = await connection.scalar(
                delete(GitHubInstallation)
                .where(GitHubInstallation.org_id == org_id, GitHubInstallation.id == row_id)
                .returning(GitHubInstallation.installation_id)
            )
            if gone is None:
                return False
            await _audit(
                connection,
                org_id,
                user_id,
                "github.removed",
                "github_installation",
                str(row_id),
                {"installation_id": gone},
            )
        self.client.forget(int(gone))
        return True

    # -- the allowlist ----------------------------------------------------------------------

    async def repos(self, org_id: uuid.UUID) -> list[AllowedRepo]:
        async with self._engine.connect() as connection:
            rows = await connection.execute(
                _repo_query().where(GitHubRepo.org_id == org_id).order_by(GitHubRepo.full_name)
            )
            return [_allowed(row) for row in rows]

    async def allowed(self, org_id: uuid.UUID, owner: str, name: str) -> AllowedRepo | None:
        """The repository if it is on the allowlist of the org, else ``None``."""
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    _repo_query().where(
                        GitHubRepo.org_id == org_id,
                        GitHubRepo.full_name == f"{owner}/{name}".lower(),
                    )
                )
            ).first()
        return None if row is None else _allowed(row)

    async def run_problem(self, org_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
        """Why the run has no authority now, else ``None`` (the rule of the model proxy)."""
        async with self._engine.connect() as connection:
            return (await load_authority(connection, org_id, run_id)).problem

    async def add_repo(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        repo: str,
        protected_paths: Sequence[str] = (),
    ) -> AllowedRepo:
        owner, name = split_repo(repo)
        extra = normalize_paths(protected_paths)
        try:
            installation_id = await self.client.repo_installation(owner, name)
        except GitHubError as error:
            raise GitHubSetupError(str(error), 502) from None
        async with self._engine.connect() as connection:
            installation = (
                await connection.execute(
                    select(GitHubInstallation.id).where(
                        GitHubInstallation.org_id == org_id,
                        GitHubInstallation.installation_id == installation_id,
                    )
                )
            ).first()
        if installation_id is None or installation is None:
            raise GitHubSetupError(
                f"the sama GitHub App of this org cannot reach {owner}/{name}. Install the App "
                "on the account of the repository and give it access to the repository",
                409,
            )
        try:
            found = await self.client.repo(installation_id, owner, name)
        except GitHubError as error:
            raise GitHubSetupError(str(error), 502) from None
        canonical_owner = str((found.get("owner") or {}).get("login") or owner)
        canonical_name = str(found.get("name") or name)
        try:
            async with self._engine.begin() as connection:
                row_id = await connection.scalar(
                    insert(GitHubRepo)
                    .values(
                        org_id=org_id,
                        installation_ref=installation.id,
                        owner=canonical_owner,
                        name=canonical_name,
                        full_name=f"{canonical_owner}/{canonical_name}".lower(),
                        protected_paths=list(extra),
                        created_by_user_id=user_id,
                    )
                    .returning(GitHubRepo.id)
                )
                await _audit(
                    connection,
                    org_id,
                    user_id,
                    "github.repo_added",
                    "github_repo",
                    str(row_id),
                    {"repo": f"{canonical_owner}/{canonical_name}", "protected_paths": list(extra)},
                )
        except IntegrityError:
            raise GitHubSetupError("the repository is on the allowlist already", 409) from None
        assert row_id is not None  # noqa: S101 -- RETURNING gives the id
        return AllowedRepo(row_id, org_id, installation_id, canonical_owner, canonical_name, extra)

    async def remove_repo(self, org_id: uuid.UUID, user_id: uuid.UUID, repo_id: uuid.UUID) -> bool:
        async with self._engine.begin() as connection:
            gone = await connection.scalar(
                delete(GitHubRepo)
                .where(GitHubRepo.org_id == org_id, GitHubRepo.id == repo_id)
                .returning(GitHubRepo.full_name)
            )
            if gone is None:
                return False
            await _audit(
                connection,
                org_id,
                user_id,
                "github.repo_removed",
                "github_repo",
                str(repo_id),
                {"repo": gone},
            )
        return True

    # -- push grants ------------------------------------------------------------------------

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
        """Records an approved push of ``commit`` to ``ref``. Returns the end of the grant."""
        if not ref.startswith(SAMA_REF_PREFIX) or not _SHA.fullmatch(commit):
            raise GitHubSetupError("a grant needs a sama/* branch and a full commit id")
        expires = _now() + PUSH_GRANT_TTL
        async with self._engine.begin() as connection:
            grant_id = await connection.scalar(
                insert(GitHubPushGrant)
                .values(
                    org_id=repo.org_id,
                    repo_id=repo.id,
                    ref=ref,
                    commit_sha=commit,
                    session_id=session_id,
                    run_id=run_id,
                    expires_at=expires,
                )
                .returning(GitHubPushGrant.id)
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=repo.org_id,
                    actor_kind="agent",
                    actor_user_id=user_id,
                    action="github.push_granted",
                    target_type="github_repo",
                    target_id=str(repo.id),
                    run_id=run_id,
                    details={
                        "grant_id": str(grant_id),
                        "repo": repo.full_name,
                        "ref": ref,
                        "commit": commit,
                    },
                )
            )
        return expires

    async def take_grant(self, repo: AllowedRepo, ref: str, commit: str) -> bool:
        """Uses up one open grant for the push of ``commit`` to ``ref``."""
        async with self._engine.begin() as connection:
            grant = (
                await connection.execute(
                    select(GitHubPushGrant.id, GitHubPushGrant.run_id)
                    .where(
                        GitHubPushGrant.org_id == repo.org_id,
                        GitHubPushGrant.repo_id == repo.id,
                        GitHubPushGrant.ref == ref,
                        GitHubPushGrant.commit_sha == commit,
                        GitHubPushGrant.used_at.is_(None),
                        GitHubPushGrant.expires_at > _now(),
                    )
                    .order_by(GitHubPushGrant.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if grant is None:
                return False
            await connection.execute(
                update(GitHubPushGrant).where(GitHubPushGrant.id == grant.id).values(used_at=_now())
            )
            await connection.execute(
                insert(AuditEntry).values(
                    org_id=repo.org_id,
                    actor_kind="agent",
                    action="github.push_grant_used",
                    target_type="github_repo",
                    target_id=str(repo.id),
                    run_id=grant.run_id,
                    details={"grant_id": str(grant.id), "ref": ref, "commit": commit},
                )
            )
        return True


def _repo_query() -> Any:
    return select(
        GitHubRepo.id,
        GitHubRepo.org_id,
        GitHubInstallation.installation_id,
        GitHubRepo.owner,
        GitHubRepo.name,
        GitHubRepo.protected_paths,
    ).join(
        GitHubInstallation,
        (GitHubInstallation.org_id == GitHubRepo.org_id)
        & (GitHubInstallation.id == GitHubRepo.installation_ref),
    )


def _allowed(row: Any) -> AllowedRepo:
    return AllowedRepo(
        id=row.id,
        org_id=row.org_id,
        installation_id=int(row.installation_id),
        owner=row.owner,
        name=row.name,
        extra_paths=tuple(str(path) for path in row.protected_paths),
    )


async def _audit(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any],
) -> None:
    await connection.execute(
        insert(AuditEntry).values(
            org_id=org_id,
            actor_kind="user",
            actor_user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
