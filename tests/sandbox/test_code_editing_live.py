"""Code editing in an E2B sandbox (roadmap M1.16).

``repo_clone`` clones a public GitHub repository through the default egress allowlist, and
the sandbox installs a test runner from PyPI and runs a test. Another host stays blocked.
Runs with ``SAMA_LIVE_TESTS=1`` and ``E2B_API_KEY``.
"""

from __future__ import annotations

import uuid

import pytest

from sama.controlplane.settings import DEFAULT_SANDBOX_HOSTS
from sama.sandbox import Sandbox, SandboxSpec
from sama.tools import RepoCloner, RepoError
from tests.sandbox.test_providers import _CURL, E2B_KEY, E2B_TEMPLATE, LIVE_E2B
from tests.support import make_claims

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_E2B, reason="set SAMA_LIVE_TESTS=1 and E2B_API_KEY"),
]

PUBLIC_REPO = "https://github.com/octocat/Hello-World"


async def test_the_agent_can_clone_change_and_test_a_public_repository() -> None:
    from sama.sandbox.e2b import E2BProvider

    claims = make_claims(org="org-test", session=f"sess-{uuid.uuid4().hex[:8]}")
    sandbox = await E2BProvider(api_key=E2B_KEY).create(
        SandboxSpec(
            org_id=claims.org_id,
            session_id=claims.session_id,
            template=E2B_TEMPLATE,
            allow_out=DEFAULT_SANDBOX_HOSTS,
        )
    )

    async def session_sandbox(_org_id: str, _session_id: str) -> Sandbox | None:
        return sandbox

    try:
        cloner = RepoCloner(session_sandbox)
        cloned = await cloner.clone(claims, PUBLIC_REPO)
        with pytest.raises(RepoError, match="does not exist or is not public"):
            await cloner.clone(claims, "octocat/this-repository-does-not-exist-sama")
        script = (
            "set -e\n"
            "printf 'def test_readme():\\n    assert open(\"README\").read()\\n' > test_readme.py\n"
            "pip install --quiet --disable-pip-version-check pytest\n"
            "python3 -m pytest -q test_readme.py\n"
            "git status --porcelain\n"
        )
        # A login shell gets the system Python with pip, as the agent shell does.
        tested = await sandbox.exec(["bash", "-lc", script], cwd=cloned.path, timeout_s=240)
        blocked = await sandbox.exec([*_CURL, "https://example.com/"], timeout_s=30)
    finally:
        await sandbox.kill()
    assert cloned.branch == "master"
    assert len(cloned.commit) == 40
    assert tested.exit_code == 0, tested.stdout + tested.stderr
    assert "1 passed" in tested.stdout
    assert "?? test_readme.py" in tested.stdout
    assert blocked.exit_code != 0
