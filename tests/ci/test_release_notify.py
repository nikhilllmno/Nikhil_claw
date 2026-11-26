"""Tests of scripts/release-notify.sh, the release notification of the deploy workflow.

The tests run the script with sample inputs against a fake GitHub API and a fake webhook on
localhost (docs/architecture.md, Section 19.3).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release-notify.sh"
REPO = "acme/sama"
SECRET_PATH = "/hook/very-secret-token-123"
SHA_NEW = "c" * 40
SHA_OLD = "a" * 40
SHA_FAILED = "b" * 40
PAYLOAD_KEYS = {
    "event",
    "status",
    "environment",
    "service",
    "repository",
    "version",
    "ref",
    "commit_sha",
    "previous_commit_sha",
    "compare_url",
    "run_url",
    "run_id",
    "run_attempt",
    "actor",
    "deployed_at",
}

BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "jq", "curl", "git")),
    reason="needs bash, jq, curl, and git",
)


@dataclass
class FakeGitHub:
    """The answers of the fake GitHub API and webhook, and the requests that they got."""

    deployments: Any = field(default_factory=list)
    deployments_code: int = 200
    statuses: dict[int, list[dict[str, str]]] = field(default_factory=dict)
    compare_status: str = "ahead"
    hook_code: int = 202
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    posts: list[bytes] = field(default_factory=list)

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.requests]


def _handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            fake.requests.append(("GET", self.path, dict(self.headers)))
            path = self.path.split("?", 1)[0]
            match = re.fullmatch(rf"/repos/{REPO}/deployments/(\d+)/statuses", path)
            if match:
                self._send(200, fake.statuses.get(int(match.group(1)), []))
            elif path == f"/repos/{REPO}/deployments":
                self._send(fake.deployments_code, fake.deployments)
            elif path.startswith(f"/repos/{REPO}/compare/"):
                self._send(200, {"status": fake.compare_status})
            else:
                self._send(404, {"message": "Not Found"})

        def do_POST(self) -> None:
            fake.requests.append(("POST", self.path, dict(self.headers)))
            length = int(self.headers.get("Content-Length", "0"))
            fake.posts.append(self.rfile.read(length))
            self._send(fake.hook_code, {})

    return Handler


@pytest.fixture
def server() -> Iterator[tuple[FakeGitHub, str]]:
    fake = FakeGitHub()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def run_script(base: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/"),
        "RELEASE_WEBHOOK_URL": f"{base}{SECRET_PATH}",
        "GH_TOKEN": "fake-gh-token",
        "DEPLOY_STATUS": "success",
        "DEPLOY_ENVIRONMENT": "production",
        "DEPLOY_SERVICE": "sama",
        "DEPLOY_SHA": SHA_NEW,
        "DEPLOY_REF": "refs/heads/main",
        "DEPLOY_IMAGE_LABEL": f"main-{SHA_NEW}",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_API_URL": base,
        "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_ACTOR": "octo-pusher",
        "GITHUB_TRIGGERING_ACTOR": "octo-deployer",
    }
    env.update(overrides)
    env = {key: value for key, value in env.items() if value != "<unset>"}
    result = subprocess.run(
        [BASH, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # The script never fails the deploy, and it never prints the URL.
    assert result.returncode == 0, result.stderr
    assert SECRET_PATH not in result.stdout + result.stderr
    assert "very-secret-token" not in result.stdout + result.stderr
    return result


def only_payload(fake: FakeGitHub) -> dict[str, Any]:
    assert len(fake.posts) == 1
    body = fake.posts[0]
    assert len(body) < 2048
    payload: dict[str, Any] = json.loads(body)
    assert set(payload) == PAYLOAD_KEYS
    return payload


SHA_MAIN_TIP = "d" * 40


def url_of(sha: str) -> str:
    return f"https://sama-production.fly.dev/#main-{sha}"


def success(sha: str) -> dict[str, str]:
    return {"state": "success", "environment_url": url_of(sha)}


def standard_history(fake: FakeGitHub) -> None:
    """Newest first: this job (in progress), a failed deploy, and an older good deploy.

    GitHub records the tip of main as the SHA of each deployment, so the deployed commit is
    only in the environment URL.
    """
    fake.deployments = [
        {"id": 30, "sha": SHA_MAIN_TIP},
        {"id": 20, "sha": SHA_MAIN_TIP},
        {"id": 10, "sha": SHA_MAIN_TIP},
    ]
    fake.statuses = {
        30: [{"state": "in_progress"}, {"state": "queued"}],
        20: [{"state": "failure", "environment_url": url_of(SHA_FAILED)}],
        10: [{"state": "inactive", "environment_url": ""}, success(SHA_OLD)],
    }


def git(repo: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(repo),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    out = subprocess.run(
        [GIT, "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, list[str]]:
    """A clone with three commits. The first one has the annotated tag v0.1.0."""
    repo = tmp_path / "clone"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    shas = []
    for n in range(3):
        git(repo, "commit", "-q", "--allow-empty", "-m", f"commit {n}")
        shas.append(git(repo, "rev-parse", "HEAD"))
        if n == 0:
            git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0")
    return repo, shas


def test_no_secret_sends_nothing(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    result = run_script(base, RELEASE_WEBHOOK_URL="")
    assert "not set" in result.stdout
    assert fake.requests == []


def test_successful_deploy(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    standard_history(fake)
    result = run_script(base)

    payload = only_payload(fake)
    assert payload == {
        "event": "deploy",
        "status": "succeeded",
        "environment": "production",
        "service": "sama",
        "repository": REPO,
        "version": f"main-{SHA_NEW}",
        "ref": "refs/heads/main",
        "commit_sha": SHA_NEW,
        "previous_commit_sha": SHA_OLD,
        "compare_url": f"https://github.com/{REPO}/compare/{SHA_OLD}...{SHA_NEW}",
        "run_url": f"https://github.com/{REPO}/actions/runs/123456789",
        "run_id": 123456789,
        "run_attempt": 2,
        "actor": "octo-deployer",
        "deployed_at": payload["deployed_at"],
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", payload["deployed_at"])
    assert "source: deployments" in result.stdout
    assert "HTTP 202" in result.stdout

    gets = [(path, headers) for method, path, headers in fake.requests if method == "GET"]
    assert gets[0][0] == f"/repos/{REPO}/deployments?environment=production&per_page=20"
    assert all(h.get("Authorization") == "Bearer fake-gh-token" for _, h in gets)
    posts = [(path, h) for method, path, h in fake.requests if method == "POST"]
    assert posts[0][0] == SECRET_PATH
    assert posts[0][1]["Content-Type"] == "application/json"


@pytest.mark.parametrize(
    ("job_status", "expected"),
    [("success", "succeeded"), ("failure", "failed"), ("cancelled", "cancelled"), ("", "failed")],
)
def test_status_mapping(server: tuple[FakeGitHub, str], job_status: str, expected: str) -> None:
    fake, base = server
    run_script(base, DEPLOY_STATUS=job_status, DEPLOY_ENVIRONMENT="staging")
    payload = only_payload(fake)
    assert payload["status"] == expected
    assert payload["environment"] == "staging"


def test_rollback_from_compare_api(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    standard_history(fake)
    fake.compare_status = "behind"
    run_script(base)

    payload = only_payload(fake)
    assert payload["event"] == "rollback"
    assert payload["previous_commit_sha"] == SHA_OLD
    # The link shows the commits that are no longer live.
    assert payload["compare_url"] == f"https://github.com/{REPO}/compare/{SHA_NEW}...{SHA_OLD}"
    assert any(p.startswith(f"/repos/{REPO}/compare/{SHA_OLD}...{SHA_NEW}") for p in fake.paths())


def test_rollback_from_local_history(
    server: tuple[FakeGitHub, str], history: tuple[Path, list[str]]
) -> None:
    fake, base = server
    repo, shas = history
    fake.deployments = [{"id": 2, "sha": shas[2]}, {"id": 1, "sha": shas[2]}]
    fake.statuses = {2: [{"state": "in_progress"}], 1: [success(shas[2])]}
    run_script(
        base,
        DEPLOY_SHA=shas[0],
        DEPLOY_IMAGE_LABEL=f"main-{shas[0]}",
        RELEASE_NOTIFY_GIT_DIR=str(repo),
    )

    payload = only_payload(fake)
    assert payload["event"] == "rollback"
    assert payload["commit_sha"] == shas[0]
    assert payload["previous_commit_sha"] == shas[2]
    # A tag on the deployed commit is the version.
    assert payload["version"] == "v0.1.0"
    assert not any("/compare/" in p for p in fake.paths())


def test_previous_tag_when_the_deployments_api_fails(
    server: tuple[FakeGitHub, str], history: tuple[Path, list[str]]
) -> None:
    fake, base = server
    repo, shas = history
    fake.deployments_code = 500
    fake.deployments = {"message": "Server Error"}
    result = run_script(
        base,
        DEPLOY_SHA=shas[2],
        DEPLOY_IMAGE_LABEL=f"main-{shas[2]}",
        RELEASE_NOTIFY_GIT_DIR=str(repo),
    )

    payload = only_payload(fake)
    assert "source: tag" in result.stdout
    assert payload["event"] == "deploy"
    assert payload["previous_commit_sha"] == shas[0]
    assert payload["version"] == f"main-{shas[2]}"
    assert payload["compare_url"] == f"https://github.com/{REPO}/compare/{shas[0]}...{shas[2]}"


def test_unmarked_deployment_uses_the_previous_tag(
    server: tuple[FakeGitHub, str], history: tuple[Path, list[str]]
) -> None:
    """The last good deploy is from before the URL marker: do not trust its SHA."""
    fake, base = server
    repo, shas = history
    fake.deployments = [{"id": 2, "sha": shas[2]}, {"id": 1, "sha": shas[1]}, {"id": 0}]
    fake.statuses = {
        2: [{"state": "in_progress"}],
        1: [{"state": "success", "environment_url": "https://sama-production.fly.dev"}],
        0: [success(SHA_OLD)],
    }
    result = run_script(
        base,
        DEPLOY_SHA=shas[2],
        DEPLOY_IMAGE_LABEL=f"main-{shas[2]}",
        RELEASE_NOTIFY_GIT_DIR=str(repo),
    )
    payload = only_payload(fake)
    assert "source: tag" in result.stdout
    assert payload["previous_commit_sha"] == shas[0]


def test_no_previous_deployment(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    fake.deployments = [{"id": 1, "sha": SHA_NEW}]
    fake.statuses = {1: [{"state": "in_progress"}]}
    result = run_script(base)

    payload = only_payload(fake)
    assert "source: none" in result.stdout
    assert payload["event"] == "deploy"
    assert payload["previous_commit_sha"] is None
    assert payload["compare_url"] is None


def test_bad_sha_is_not_sent_or_looked_up(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    run_script(base, DEPLOY_SHA='abc"; echo pwned', DEPLOY_STATUS="failure")

    payload = only_payload(fake)
    assert payload["commit_sha"] is None
    assert payload["status"] == "failed"
    assert not any(method == "GET" for method, _, _ in fake.requests)


def test_values_are_json_escaped(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    run_script(base, GITHUB_TRIGGERING_ACTOR='evil"actor\\', DEPLOY_REF="refs/heads/a\nb")
    payload = only_payload(fake)
    assert payload["actor"] == 'evil"actor\\'
    assert payload["ref"] == "refs/heads/a\nb"


def test_webhook_error_does_not_fail(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    fake.hook_code = 500
    result = run_script(base)
    assert len(fake.posts) == 1
    assert "HTTP 500" in result.stdout


def test_unreachable_webhook_does_not_fail(server: tuple[FakeGitHub, str]) -> None:
    _, base = server
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    result = run_script(base, RELEASE_WEBHOOK_URL=f"http://127.0.0.1:{closed_port}{SECRET_PATH}")
    assert "HTTP 000" in result.stdout


def test_unreachable_api_still_notifies(server: tuple[FakeGitHub, str]) -> None:
    fake, base = server
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    run_script(base, GITHUB_API_URL=f"http://127.0.0.1:{closed_port}", GH_TOKEN="<unset>")
    payload = only_payload(fake)
    assert payload["previous_commit_sha"] is None
