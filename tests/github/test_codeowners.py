"""The code owners file of a repository (docs/architecture.md, Section 15.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sama.github.codeowners import parse_codeowners, split_owners

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*", "a/b/c.py", True),
        ("*.js", "app.js", True),
        ("*.js", "web/src/app.js", True),
        ("*.js", "app.jsx", False),
        ("/build/", "build/out/x.txt", True),
        ("/build/", "src/build/x.txt", False),
        ("/build/", "build", False),
        ("apps/", "apps/a.py", True),
        ("apps/", "x/apps/y/a.py", True),
        ("apps/", "apps", False),
        ("docs/*", "docs/start.md", True),
        ("docs/*", "docs/build/trouble.md", False),
        ("docs/*", "x/docs/start.md", False),
        ("**/logs", "logs/a.log", True),
        ("**/logs", "a/b/logs/c.log", True),
        ("**/logs", "a/logsx/c.log", False),
        ("/src/sama/gate", "src/sama/gate/decide.py", True),
        ("/src/sama/gate", "src/sama/gate", True),
        ("/src/sama/gate", "src/sama/gateway/x.py", False),
        ("src/sama/gate/", "src/sama/gate/decide.py", True),
        ("src/sama/gate/", "other/src/sama/gate/decide.py", False),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/x/y/c", False),
        ("a/**", "a/x/y", True),
        ("a/**", "b/a/x", False),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("/.github/", ".github/workflows/ci.yml", True),
        ("CODEOWNERS", ".github/CODEOWNERS", True),
        ("/Docs/", "docs/a.md", False),
        ("a.b", "axb", False),
    ],
)
def test_a_pattern_matches_as_on_github(pattern: str, path: str, expected: bool) -> None:
    owners = parse_codeowners(f"{pattern} @owner")
    assert owners.owners_of(path) == (("@owner",) if expected else ())


def test_the_last_matching_line_wins() -> None:
    owners = parse_codeowners(
        "\n".join(
            [
                "# The owners",
                "*          @everyone   # a comment",
                "/src/      @core",
                "/src/sama/gate/ @owner @acme/security",
                "/src/sama/gate/docs.md",
                "",
            ]
        )
    )
    assert owners.owners_of("README.md") == ("@everyone",)
    assert owners.owners_of("src/sama/x.py") == ("@core",)
    assert owners.owners_of("src/sama/gate/decide.py") == ("@owner", "@acme/security")
    assert owners.owners_of("src/sama/gate/docs.md") == (), "a line without owners clears them"


def test_unsupported_lines_are_skipped() -> None:
    owners = parse_codeowners("!/secret @a\n/x[ab]/ @b\n/ok/ @c\n/bad/ not-an-owner\n\\#hash @d\n")
    assert owners.skipped == (1, 2, 4)
    assert [rule.pattern for rule in owners.rules] == ["/ok/", "#hash"]
    assert owners.owners_of("#hash") == ("@d",)


def test_owners_split_into_users_and_teams() -> None:
    users, teams = split_owners(
        ["@alice", "@acme/security", "bob@example.com", "@alice", "@acme/security", "bad"]
    )
    assert users == ("alice",)
    assert teams == ("security",)


def test_the_sama_repo_protects_its_sensitive_paths() -> None:
    """The code owners file of this repository names an owner for each protected area of
    Section 15.2: auth, tenancy, approvals, the model proxy, the sandbox, and CI."""
    owners = parse_codeowners((REPO_ROOT / ".github" / "CODEOWNERS").read_text())
    assert not owners.skipped
    for path in (
        ".github/workflows/ci.yml",
        ".github/CODEOWNERS",
        "src/sama/api/auth.py",
        "src/sama/tenancy/run_token.py",
        "src/sama/gate/decide.py",
        "src/sama/modelproxy/app.py",
        "src/sama/sandbox/e2b.py",
        "sandbox/Dockerfile",
        "src/sama/vault/crypto.py",
        "src/sama/github/proxy.py",
        "src/sama/tools/sama_pr.py",
        "migrations/versions/0001_tenancy_and_sessions.py",
        "deploy/fly.production.toml",
    ):
        assert owners.owners_of(path), path
    assert owners.owners_of("src/sama/memory/store.py") == ()
    assert owners.owners_of("docs/roadmap.md") == ()
