"""Import a skill from a public GitHub repository (docs/architecture.md, Section 9.6; roadmap
M4.10).

``skill_import`` reads one skill folder of a repository at one commit: the ``SKILL.md`` and
the files in ``references/``, ``templates/``, ``scripts/``, and ``assets/``. Other files are
left out. The import has three checks:

1. The tool gate asks a person to approve the call, because ``skill_import`` is not allowed
   by default (Section 16.1).
2. The scan of this module refuses a script that runs code from the network, decodes code and
   runs it, or deletes a home or root folder. The lint of every skill write then refuses
   prompt injection, hidden text, and secrets (Section 9.2).
3. The write is a normal ``create``: a workspace or org skill from a chat is a proposal that a
   person approves (Section 9.4).

Only ``api.github.com`` and ``raw.githubusercontent.com`` are reached, with the address rules
of ``web_extract``. No token is sent, so only public repositories work.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, urlsplit

from sama.gate.hardline import MAX_COMMAND_CHARS, command_problem
from sama.skills.errors import SkillError
from sama.skills.format import parse
from sama.skills.library import ManageRequest
from sama.skills.lint import FILE_FOLDERS, MAX_FILE_CHARS, MAX_FILES, SKILL_FILE, name_problem
from sama.skills.store import SkillStore, Written
from sama.tenancy.run_token import RunTokenClaims
from sama.tools.outbound import BlockedUrlError, FetchError, OutboundClient, UrlPolicy
from sama.tools.repos import RepoError, RepoRef, parse_repo
from sama.vault.redact import redact

API_URL: Final = "https://api.github.com"
RAW_URL: Final = "https://raw.githubusercontent.com"
HOSTS: Final = ("api.github.com", "raw.githubusercontent.com")
POLICY: Final = UrlPolicy(allow_hosts=HOSTS, max_redirects=0, max_bytes=5 * 1024 * 1024)
"""No redirect: a moved repository gives an error, not a read of another host."""
MAX_PATH_CHARS: Final = 200
FETCH_CONCURRENCY: Final = 4
_REF: Final = re.compile(r"[A-Za-z0-9._/-]{1,100}")
_SEGMENT: Final = re.compile(r"[A-Za-z0-9._-]+")

IMPORT_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "remote_script": re.compile(
        r"\b(curl|wget)\b[^\n|;]{0,200}\|\s*(sudo\s+)?(ba|z|da|k)?sh\b"
        r"|\b(ba|z)?sh\s+<\(\s*(curl|wget)\b",
        re.IGNORECASE,
    ),
    "encoded_code": re.compile(
        r"\bbase64\s+(-d|--decode|-D)\b[^\n]{0,100}\|\s*(sudo\s+)?(ba|z)?sh\b"
        r"|\bexec\s*\(\s*(base64\.)?b64decode\b",
        re.IGNORECASE,
    ),
}
"""Patterns that a shared skill should not have. Each has bounded repeats."""

_LINE_PREFIX: Final = re.compile(r"^\s*(?:(?:[-*>#]+|\d+\.|\$)\s+)*")
"""A list mark, a quote mark, a heading mark, or a shell prompt before a command."""


def _destructive(text: str) -> bool:
    """True if a line of the text is a command on the hardline list of the gate, for example a
    recursive delete of ``/``, ``~``, or ``"$HOME"`` with any form of the flags."""
    for line in text.splitlines():
        command = _LINE_PREFIX.sub("", line)
        if command and len(command) <= MAX_COMMAND_CHARS and command_problem(command):
            return True
    return False


@dataclass(frozen=True, slots=True)
class SkillSource:
    """One skill folder in a GitHub repository."""

    repo: RepoRef
    ref: str
    path: str
    """The folder of ``SKILL.md`` in the repository. Empty for the root."""

    @property
    def label(self) -> str:
        where = f"/{self.path}" if self.path else ""
        return f"github.com/{self.repo.owner}/{self.repo.name}{where}"


def parse_source(url: str, *, path: str | None = None, ref: str | None = None) -> SkillSource:
    """The skill folder of ``https://github.com/<owner>/<repo>`` or of
    ``https://github.com/<owner>/<repo>/tree/<ref>/<path>``. ``path`` and ``ref`` replace the
    parts of the URL. Raises ``SkillError``."""
    raw = url.strip()
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    segments = [s for s in parts.path.split("/") if s]
    url_ref: str | None = None
    url_path = ""
    if len(segments) > 2:
        if len(segments) < 4 or segments[2] != "tree":
            raise SkillError(
                "Give the skill as https://github.com/<owner>/<repo> or "
                "https://github.com/<owner>/<repo>/tree/<ref>/<folder>."
            )
        url_ref = segments[3]
        url_path = "/".join(segments[4:])
        raw = f"{parts.scheme}://{parts.netloc}/{segments[0]}/{segments[1]}"
    try:
        repo = parse_repo(raw)
    except RepoError as error:
        raise SkillError(str(error)) from None
    chosen_ref = (ref or url_ref or "HEAD").strip()
    if not _REF.fullmatch(chosen_ref) or ".." in chosen_ref:
        raise SkillError("The ref must be a branch, a tag, or a commit SHA.")
    folder = (path if path is not None else url_path).strip().strip("/")
    if len(folder) > MAX_PATH_CHARS or any(
        not _SEGMENT.fullmatch(s) or s in (".", "..") for s in folder.split("/") if folder
    ):
        raise SkillError("The path must be a folder in the repository, like skills/pdf.")
    return SkillSource(repo, chosen_ref, folder)


@dataclass(frozen=True, slots=True)
class FetchedSkill:
    source: SkillSource
    commit: str
    skill_md: str
    files: dict[str, str]
    skipped: tuple[str, ...]
    """Files of the folder that are not part of a skill, for example a README or a LICENSE."""


def scan_import(path: str, text: str) -> list[str]:
    """The import findings of one file, as ``<path>: <ids>``."""
    found = [name for name, pattern in IMPORT_PATTERNS.items() if pattern.search(text)]
    if _destructive(text):
        found.append("destructive")
    return [f"{redact(path)}: {', '.join(found)}"] if found else []


class SkillImporter:
    """Reads a skill from GitHub and writes it with ``SkillStore.manage``."""

    def __init__(self, store: SkillStore, *, outbound: OutboundClient | None = None) -> None:
        self._store = store
        self._outbound = outbound or OutboundClient(POLICY)

    async def aclose(self) -> None:
        await self._outbound.aclose()

    async def run(
        self,
        claims: RunTokenClaims,
        url: str,
        *,
        path: str | None = None,
        ref: str | None = None,
        name: str | None = None,
        scope: str | None = None,
    ) -> str:
        """The ``skill_import`` tool: import, then the text for the model."""
        source = parse_source(url, path=path, ref=ref)
        written, fetched = await self.import_skill(claims, source, name=name, scope=scope)
        return render_import(written, fetched)

    async def import_skill(
        self,
        claims: RunTokenClaims,
        source: SkillSource,
        *,
        name: str | None = None,
        scope: str | None = None,
    ) -> tuple[Written, FetchedSkill]:
        fetched = await self.fetch(source)
        findings: list[str] = []
        for path, text in ((SKILL_FILE, fetched.skill_md), *fetched.files.items()):
            findings += scan_import(path, text)
        if findings:
            listed = "".join(f"\n- {finding}" for finding in findings[:6])
            raise SkillError(f"The scan refused the skill. Nothing was saved:{listed}")
        chosen = (name or _frontmatter_name(fetched.skill_md) or _folder_name(source)).strip()
        if (problem := name_problem(chosen)) is not None:
            raise SkillError(f"{problem} Give a name with the argument name.")
        request = ManageRequest(
            action="create",
            name=chosen,
            scope=scope,
            content=_with_name(fetched.skill_md, chosen),
            files=fetched.files,
            summary=f"Imported from {source.label} at {fetched.commit[:12]}",
        )
        return await self._store.manage(claims, request), fetched

    async def fetch(self, source: SkillSource) -> FetchedSkill:
        repo = f"{quote(source.repo.owner)}/{quote(source.repo.name)}"
        commit_data = await self._json(f"{API_URL}/repos/{repo}/commits/{quote(source.ref)}")
        commit = _string(commit_data, "sha")
        tree_sha = _string(_dict(_dict(commit_data, "commit"), "tree"), "sha")
        tree = await self._json(f"{API_URL}/repos/{repo}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise SkillError("The repository is too large to read. Import from a smaller one.")
        prefix = f"{source.path}/" if source.path else ""
        wanted: list[str] = []
        skipped: list[str] = []
        has_skill = False
        entries = tree.get("tree")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("type") != "blob":
                continue
            full = entry.get("path")
            if not isinstance(full, str) or not full.startswith(prefix):
                continue
            relative = full[len(prefix) :]
            if relative == SKILL_FILE:
                has_skill = True
            elif relative.split("/", 1)[0] in FILE_FOLDERS and "/" in relative:
                size = entry.get("size")
                if isinstance(size, int) and size > MAX_FILE_CHARS * 4:
                    skipped.append(relative)
                else:
                    wanted.append(relative)
            else:
                skipped.append(relative)
        if not has_skill:
            raise SkillError(f"There is no {SKILL_FILE} in {source.label}.")
        if len(wanted) > MAX_FILES:
            raise SkillError(f"The skill has more than {MAX_FILES} files.")
        limit = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def raw(relative: str) -> bytes:
            async with limit:
                url = f"{RAW_URL}/{repo}/{commit}/{quote(prefix + relative)}"
                return await self._get(url)

        skill_md, *contents = await asyncio.gather(raw(SKILL_FILE), *(raw(p) for p in wanted))
        files: dict[str, str] = {}
        for relative, data in zip(wanted, contents, strict=True):
            try:
                files[relative] = data.decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(relative)
        try:
            text = skill_md.decode("utf-8")
        except UnicodeDecodeError:
            raise SkillError(f"{SKILL_FILE} is not UTF-8 text.") from None
        return FetchedSkill(source, commit, text, files, tuple(sorted(skipped)))

    async def _get(self, url: str) -> bytes:
        try:
            result = await self._outbound.get(url)
        except (BlockedUrlError, FetchError) as error:
            raise SkillError(f"GitHub could not be read: {error}") from None
        if result.status_code == 404:
            raise SkillError("GitHub has no such public repository, ref, or file.")
        if result.status_code in (403, 429):
            raise SkillError("GitHub refused the request. It can be a rate limit: try later.")
        if result.status_code != 200:
            raise SkillError(f"GitHub returned status {result.status_code}.")
        if result.truncated:
            raise SkillError("A file of the skill is too large.")
        return result.content

    async def _json(self, url: str) -> dict[str, Any]:
        try:
            data = json.loads(await self._get(url))
        except ValueError:
            raise SkillError("GitHub returned a response that is not JSON.") from None
        if not isinstance(data, dict):
            raise SkillError("GitHub returned an unexpected response.")
        return data


def _dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise SkillError("GitHub returned an unexpected response.")
    return value


def _string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise SkillError("GitHub returned an unexpected response.")
    return value


def _frontmatter_name(skill_md: str) -> str | None:
    try:
        return parse(skill_md).meta.name or None
    except SkillError:
        return None


_NAME_LINE: Final = re.compile(r"name\s*:.*")


def _with_name(skill_md: str, name: str) -> str:
    """The ``SKILL.md`` with ``name`` as the frontmatter name. The other lines do not change.
    The store checks that the two names are the same, so a new name changes the frontmatter
    too. A text without a frontmatter does not change, and the store reports the problem."""
    if _frontmatter_name(skill_md) == name:
        return skill_md
    lines = skill_md.split("\n")
    if lines[0].lstrip("\ufeff").rstrip() != "---":
        return skill_md
    for index in range(1, len(lines)):
        line = lines[index]
        if line.rstrip() == "---":
            # No top-level name line: add one at the start of the frontmatter.
            return "\n".join([lines[0], f"name: {name}", *lines[1:]])
        if _NAME_LINE.fullmatch(line.rstrip("\r")):
            lines[index] = f"name: {name}"
            return "\n".join(lines)
    return skill_md


def _folder_name(source: SkillSource) -> str:
    return posixpath.basename(source.path) if source.path else source.repo.name.lower()


def render_import(written: Written, fetched: FetchedSkill) -> str:
    """The result of ``skill_import``."""
    saved = (
        f"Proposed the {written.scope} skill {written.name}. A person must approve it first."
        if written.proposed
        else f'Imported the skill "{written.name}" as version {written.version} in the scope '
        f"{written.scope}. A new session sees it."
    )
    lines = [
        saved,
        f"Source: {fetched.source.label} at commit {fetched.commit[:12]}.",
        f"Files: {SKILL_FILE}" + "".join(f", {path}" for path in sorted(fetched.files)) + ".",
    ]
    if fetched.skipped:
        shown = ", ".join(redact(path) for path in fetched.skipped[:10])
        lines.append(f"Left out (not part of a skill): {shown}.")
    return "\n".join(lines)
