"""The connector catalog (docs/architecture.md, Section 10.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sama.connectors.manifest import (
    ENV_DIRECTORY,
    TOKEN_FIELD,
    Catalog,
    ManifestError,
    catalog_directory,
    mcp_fields,
    parse,
)
from sama.gate.decide import TOOLSETS

ROOT = Path(__file__).resolve().parents[2]

FORM = """
name: acme
title: Acme
description: The Acme API.
kind: http_api
fields:
- name: token
  label: Token
  secret: true
- name: team
  label: Team
  required: false
http:
  base_url: https://api.acme.test
  auth: {header: Authorization, field: token, prefix: 'Bearer '}
  test: {method: GET, path: /me}
tools:
- name: acme_get_item
  access: read
  description: Read one item.
  request: {method: GET, path: '/items/{item_id}'}
  input_schema:
    type: object
    properties: {item_id: {type: string}}
    required: [item_id]
"""


def test_the_catalog_of_the_repo_loads() -> None:
    catalog = Catalog.load(ROOT / "connectors" / "catalog")
    names = {m.name for m in catalog}
    assert {"gmail", "googlecalendar", "github", "slack", "linear", "notion"} <= names
    assert {"deepwiki", "neon-mcp", "sentry", "supabase-mcp", "stripe"} <= names
    for manifest in catalog:
        if manifest.kind == "mcp_remote":
            # A remote MCP server gives its tools at check time.
            assert manifest.mcp is not None, manifest.name
            assert not manifest.tools, manifest.name
            assert manifest.mcp.url.startswith("https://"), manifest.name
            continue
        assert manifest.tools, manifest.name
        accesses = {t.access for t in manifest.tools}
        assert "read" in accesses, manifest.name
        for tool in manifest.tools:
            assert tool.input_schema.get("type") == "object"
            assert catalog.tool(tool.name) == (manifest, tool)
    # The tool names do not hide a sama tool.
    sama_tools = {name for tools in TOOLSETS.values() for name in tools}
    assert not catalog.tool_names() & sama_tools


def test_the_default_folder_is_the_catalog_of_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_DIRECTORY, raising=False)
    assert catalog_directory() == ROOT / "connectors" / "catalog"
    monkeypatch.setenv(ENV_DIRECTORY, "/opt/x")
    assert catalog_directory() == Path("/opt/x")


def test_a_missing_folder_is_an_empty_catalog(tmp_path: Path) -> None:
    assert len(Catalog.load(tmp_path / "none")) == 0


def test_a_form_manifest_parses_and_hides_nothing_secret() -> None:
    manifest = parse(FORM)
    assert [f.name for f in manifest.secret_fields] == ["token"]
    public = manifest.public()
    assert public["fields"][0]["secret"] is True
    assert "input_schema" not in public["tools"][0]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (("acme_get_item", "other_get_item"), "starts with the connector name"),
        (("https://api.acme.test", "http://api.acme.test"), "base_url"),
        (("https://api.acme.test", "https://api.acme.test/v1"), "base_url"),
        (("field: token", "field: team"), "auth field"),
        (("required: [item_id]", "required: []"), "required argument"),
        (("kind: http_api", "kind: plugin"), "kind"),
        (("access: read", "access: maybe"), "access"),
    ],
)
def test_a_bad_manifest_is_refused(change: tuple[str, str], error: str) -> None:
    with pytest.raises(ManifestError, match=error):
        parse(FORM.replace(*change))


def test_unknown_keys_are_refused() -> None:
    with pytest.raises(ManifestError):
        parse(FORM + "extra: 1\n")


def test_the_file_name_must_be_the_connector_name(tmp_path: Path) -> None:
    (tmp_path / "other.yaml").write_text(FORM)
    with pytest.raises(ManifestError, match="file name"):
        Catalog.load(tmp_path)


def test_two_connectors_cannot_share_a_tool(tmp_path: Path) -> None:
    (tmp_path / "acme.yaml").write_text(FORM.replace("acme_get_item", "acme_two_get_item"))
    (tmp_path / "acme_two.yaml").write_text(
        FORM.replace("name: acme\n", "name: acme_two\n").replace(
            "acme_get_item", "acme_two_get_item"
        )
    )
    with pytest.raises(ManifestError, match="acme_two_get_item"):
        Catalog.load(tmp_path)


MCP = """
name: remote
title: Remote
description: A remote MCP server.
kind: mcp_remote
mcp:
  url: https://mcp.remote.test/mcp
  auth: bearer
  include: ["get_*", "list_*"]
  exclude: ["*_secret"]
  access: {"list_*": read}
"""


def test_an_mcp_manifest_gets_its_form_from_the_auth() -> None:
    manifest = parse(MCP)
    assert manifest.mcp is not None
    assert manifest.mcp.include == ("get_*", "list_*")
    assert [(f.name, f.secret, f.required) for f in manifest.fields] == [(TOKEN_FIELD, True, True)]
    assert manifest.public()["mcp"]["url"] == "https://mcp.remote.test/mcp"
    oauth = parse(MCP.replace("auth: bearer", "auth: oauth"))
    assert oauth.fields == ()
    header = parse(MCP.replace("auth: bearer", "auth: header\n  header: X-Api-Key"))
    assert header.fields == mcp_fields("header", "X-Api-Key")
    assert "X-Api-Key" in header.fields[0].help


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (("https://mcp.remote.test/mcp", "http://mcp.remote.test/mcp"), "url"),
        (("https://mcp.remote.test/mcp", "https://mcp.remote.test/mcp?x=1"), "url"),
        (("auth: bearer", "auth: header"), "header"),
        (("auth: bearer", "auth: bearer\n  header: X-Key"), "header"),
        (("auth: bearer", "auth: header\n  header: Mcp-Session-Id"), "sama sets"),
        (("auth: bearer", "auth: basic"), "auth"),
        (('"*_secret"', '"bad pattern"'), "pattern"),
        (('list_*": read', 'list_*": maybe'), "access"),
        (("kind: mcp_remote", "kind: http_api"), "mcp"),
    ],
)
def test_a_bad_mcp_manifest_is_refused(change: tuple[str, str], error: str) -> None:
    with pytest.raises(ManifestError, match=error):
        parse(MCP.replace(*change))


def test_an_mcp_manifest_cannot_list_tools_or_a_form() -> None:
    tool = (
        "tools:\n- name: remote_x\n  access: read\n  description: X.\n"
        "  input_schema: {type: object}\n"
    )
    with pytest.raises(ManifestError, match="from the server"):
        parse(MCP + tool)
    form = "fields:\n- name: key\n  label: Key\n  secret: true\n"
    with pytest.raises(ManifestError, match="token"):
        parse(MCP + form)


def test_the_catalog_refuses_a_custom_manifest(tmp_path: Path) -> None:
    (tmp_path / "remote.yaml").write_text(MCP + "custom: true\n")
    with pytest.raises(ManifestError, match="custom"):
        Catalog.load(tmp_path)
