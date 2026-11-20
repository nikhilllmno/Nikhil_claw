"""Tests of ``python -m sama.admin github-app`` (docs/architecture.md, Section 15.1)."""

from __future__ import annotations

import html
import json
import re
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from sama.admin import github_app
from sama.admin.__main__ import main
from sama.github.client import GitHubError

PEM = "-----BEGIN RSA PRIVATE KEY-----\nsecret-key-body\n-----END RSA PRIVATE KEY-----\n"
CLIENT_SECRET = "client-secret-value"
APP = {
    "id": 4242,
    "slug": "sama-test",
    "client_id": "Iv1.client",
    "html_url": "https://github.com/apps/sama-test",
    "pem": PEM,
    "client_secret": CLIENT_SECRET,
    "webhook_secret": None,
}


def _manifest_of(form: str) -> dict[str, Any]:
    match = re.search(r'name="manifest" value="([^"]*)"', form)
    assert match is not None
    manifest: dict[str, Any] = json.loads(html.unescape(match.group(1)))
    return manifest


def test_the_form_posts_the_manifest_to_the_personal_account() -> None:
    form = github_app.manifest_form("https://sama.example.test")
    assert 'action="https://github.com/settings/apps/new?state=' in form
    manifest = _manifest_of(form)
    assert manifest["redirect_url"] == "https://sama.example.test/github/app/created"
    assert manifest["callback_urls"] == ["https://sama.example.test/github/setup"]
    assert manifest["request_oauth_on_install"] is True
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["public"] is False


def test_the_form_can_post_to_an_organization_and_escapes_the_name() -> None:
    form = github_app.manifest_form("https://sama.example.test", org="acme inc", name='a"b')
    assert 'action="https://github.com/organizations/acme%20inc/settings/apps/new?state=' in form
    assert _manifest_of(form)["name"] == 'a"b'
    assert '"name"' not in form, "the manifest is escaped HTML"


def test_each_form_has_a_new_state() -> None:
    first = github_app.manifest_form("https://sama.example.test")
    second = github_app.manifest_form("https://sama.example.test")
    assert first != second


def _http(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def test_convert_writes_the_secrets_to_private_files(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    out = tmp_path / "app"
    result = github_app.convert(
        "the/code",
        out,
        fly_app="sama-staging",
        http=_http(httpx.Response(201, json=APP), seen),
        api_url="https://api.example.test",
    )
    assert [str(r.url) for r in seen] == [
        "https://api.example.test/app-manifests/the%2Fcode/conversions"
    ]
    key = Path(result["private_key_file"])
    secret = Path(result["client_secret_file"])
    assert key.read_text("utf-8") == PEM
    assert secret.read_text("utf-8") == CLIENT_SECRET
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert (result["app_id"], result["slug"], result["client_id"]) == (
        "4242",
        "sama-test",
        "Iv1.client",
    )
    text = json.dumps(result)
    assert "secret-key-body" not in text
    assert CLIENT_SECRET not in text
    command = result["next"][0]
    assert command.startswith("flyctl secrets set -a sama-staging SAMA_GITHUB_APP_ID=4242 ")
    assert f'SAMA_GITHUB_APP_PRIVATE_KEY="$(cat {key})"' in command
    assert f'SAMA_GITHUB_CLIENT_SECRET="$(cat {secret})"' in command


def test_convert_refuses_an_incomplete_app(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError, match="incomplete"):
        github_app.convert(
            "code",
            tmp_path / "app",
            http=_http(httpx.Response(201, json={**APP, "pem": ""}), seen),
        )
    assert not (tmp_path / "app").exists()


def test_convert_reports_a_github_error(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(GitHubError):
        github_app.convert(
            "used",
            tmp_path / "app",
            http=_http(httpx.Response(404, json={"message": "Not Found"}), seen),
        )


def test_the_cli_writes_the_form(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMA_PUBLIC_URL", "https://sama.example.test/")
    out = tmp_path / "form.html"
    assert main(["github-app", "manifest", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == {"form": str(out)}
    manifest = _manifest_of(out.read_text("utf-8"))
    assert manifest["url"] == "https://sama.example.test"


def test_the_cli_needs_a_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMA_PUBLIC_URL", raising=False)
    with pytest.raises(SystemExit, match="public-url"):
        main(["github-app", "manifest"])


def test_the_cli_converts_without_a_secret_in_the_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_convert(code: str, **_: Any) -> dict[str, Any]:
        assert code == "abc"
        return dict(APP)

    monkeypatch.setattr(github_app, "convert_manifest", fake_convert)
    argv = ["github-app", "convert", "abc", "--out-dir", str(tmp_path / "o"), "--fly-app", "x"]
    assert main(argv) == 0
    output = capsys.readouterr().out
    assert CLIENT_SECRET not in output
    assert "secret-key-body" not in output
    assert json.loads(output)["next"][0].startswith("flyctl secrets set -a x ")


def test_the_cli_prints_an_error_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_convert(code: str, **_: Any) -> dict[str, Any]:
        raise GitHubError("GitHub said 404: Not Found", 404)

    monkeypatch.setattr(github_app, "convert_manifest", fake_convert)
    assert main(["github-app", "convert", "abc", "--out-dir", str(tmp_path / "o")]) == 1
    assert "error: GitHub said 404" in capsys.readouterr().err


def test_the_cli_convert_needs_a_code() -> None:
    with pytest.raises(SystemExit, match="needs the code"):
        main(["github-app", "convert"])
