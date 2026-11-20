"""The database connectors without a database (docs/architecture.md, Section 10.4): the
manifest, the form settings, the parameters, TLS, and the size limits of a result."""

from __future__ import annotations

import datetime as dt
import json
import ssl
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from sama.connectors import database
from sama.connectors.database import (
    MAX_CARD_CHARS,
    MAX_MODEL_BYTES,
    MAX_ROWS,
    TIMEOUT_S,
    DatabaseGateway,
    _check_card,
    _render,
    _Rows,
)
from sama.connectors.http_api import DriverError
from sama.connectors.manifest import (
    DATABASE_OPERATIONS,
    MAX_EXECUTE_SQL_CHARS,
    Catalog,
    ManifestError,
    parse,
)
from sama.connectors.postgres import DbSettings, coerce, model_cell, pinned_tls, to_csv, writes_on
from sama.gate.decide import PolicyRule, RuleLevel, always_asks, policy_verdict
from sama.gateway.sessions import MAX_ARGUMENT_CHARS
from sama.tools.output import MAX_OUTPUT_BYTES, untrusted

ROOT = Path(__file__).resolve().parents[2]

PG = """
name: pg-main
title: Main DB
description: The main database.
kind: database
database:
  engine: postgres
  hints:
    host: The host of the main database.
"""

VALUES = {"host": "db.example.com", "database": "app", "user": "agent", "password": "pw"}


def manifest(text: str = PG) -> Any:
    return parse(text)


def test_a_database_manifest_gets_the_form_and_the_five_tools() -> None:
    found = manifest()
    assert found.kind == "database"
    names = [t.name for t in found.tools]
    assert names == [f"db_pg_main_{op}" for op in DATABASE_OPERATIONS]
    access = {t.name: t.access for t in found.tools}
    assert access["db_pg_main_execute"] == "destructive"
    assert {access[f"db_pg_main_{op}"] for op in ("schema", "sample", "query", "export")} == {
        "read"
    }
    assert [found.operation(t) for t in found.tools] == list(DATABASE_OPERATIONS)
    fields = {f.name: f for f in found.fields}
    assert [n for n, f in fields.items() if f.secret] == ["password", "ssh_private_key"]
    assert fields["host"].help == "The host of the main database."
    assert {n for n, f in fields.items() if f.required} == {"host", "database", "user", "password"}
    execute = found.tools[-1].input_schema
    assert execute["properties"]["sql"]["maxLength"] == MAX_EXECUTE_SQL_CHARS


@pytest.mark.parametrize(
    ("extra", "error"),
    [
        ("fields: [{name: x, label: X}]\n", "does not list fields or tools"),
        ("tools: [{name: x, access: read, description: d}]\n", "does not list fields or tools"),
        ("composio: {toolkit: x}\n", "database block only"),
    ],
)
def test_a_database_manifest_cannot_list_its_own_form_or_tools(extra: str, error: str) -> None:
    with pytest.raises(ManifestError, match=error):
        manifest(PG + extra)


def test_a_bad_hint_or_engine_is_refused() -> None:
    with pytest.raises(ManifestError):
        manifest(PG.replace("host: The host", "nothing: The host"))
    with pytest.raises(ManifestError):
        manifest(PG.replace("engine: postgres", "engine: oracle"))
    with pytest.raises(ManifestError):
        parse(PG.replace("kind: database", "kind: http_api"))


def test_the_catalog_has_postgres_supabase_and_neon() -> None:
    catalog = Catalog.load(ROOT / "connectors" / "catalog")
    for name, mode in (("postgres", "require"), ("supabase", "require"), ("neon", "verify-full")):
        found = catalog.get(name)
        assert found is not None
        assert found.kind == "database"
        assert found.database is not None
        assert (found.database.engine, found.database.sslmode) == ("postgres", mode)
        assert catalog.tool(f"db_{name}_query") is not None


def test_the_settings_parse_the_form() -> None:
    settings = DbSettings.parse(
        manifest(),
        {
            **VALUES,
            "host": "DB.Example.com.",
            "port": "6543",
            "schemas": "public, sales",
            "hidden_columns": "users.email",
            "writes": "on",
        },
    )
    assert (settings.host, settings.port, settings.sslmode) == ("db.example.com", 6543, "require")
    assert settings.writes
    assert not settings.policy.ddl
    assert settings.policy.column_hidden("users", "email")
    assert DbSettings.parse(manifest(), VALUES).port == 5432
    assert settings.fingerprint() != DbSettings.parse(manifest(), VALUES).fingerprint()
    assert DbSettings.parse(manifest(), {**VALUES, "host": "[2001:db8::1]"}).host == "2001:db8::1"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"host": "postgres://db.example.com/app"}, "not a URL"),
        ({"host": ""}, "host is empty"),
        ({"port": "99999"}, "1 to 65535"),
        ({"port": "x"}, "number"),
        ({"database": ""}, "required"),
        ({"sslmode": "disable"}, "require or verify-full"),
        ({"writes": "maybe"}, "on or off"),
        ({"ddl": "on"}, "needs Allow writes"),
        ({"tables": "a.b.c"}, "Allowed tables"),
        ({"hidden_columns": "x;drop"}, "Hidden columns"),
    ],
)
def test_bad_settings_are_refused(change: dict[str, str], error: str) -> None:
    with pytest.raises(DriverError, match=error):
        DbSettings.parse(manifest(), {**VALUES, **change})


def test_writes_on_reads_the_stored_field() -> None:
    assert writes_on({"writes": "On"})
    assert not writes_on({"writes": "off"})
    assert not writes_on({})
    assert not writes_on({"writes": "maybe"})


def test_params_take_the_types_of_the_statement() -> None:
    values = coerce(
        [
            "7",
            2.0,
            "1.50",
            "true",
            "2026-01-02",
            "2026-01-02T03:04:05+00:00",
            str(uuid.UUID(int=1)),
        ],
        ["int4", "int8", "numeric", "bool", "date", "timestamptz", "uuid"],
    )
    assert values[:4] == [7, 2, Decimal("1.50"), True]
    assert values[4] == dt.date(2026, 1, 2)
    assert values[5].tzinfo is not None
    assert values[6] == uuid.UUID(int=1)
    assert coerce([None, {"a": 1}, 5], ["int4", "jsonb", "text"]) == [None, '{"a": 1}', "5"]
    with pytest.raises(DriverError, match=r"params\[0\] \(\$1\) is not a valid int4"):
        coerce(["x"], ["int4"])
    with pytest.raises(DriverError, match="2 parameters"):
        coerce([1], ["int4", "int4"])


def test_the_cells_for_the_model_and_the_file() -> None:
    assert model_cell("x" * 5_000).endswith("...[cut]")
    assert len(model_cell("x" * 5_000)) < 1_100
    assert model_cell(Decimal("1.10")) == "1.10"
    assert to_csv(None) == ""
    assert to_csv({"a": 1}) == '{"a": 1}'
    assert to_csv(b"\x01") == "\\x01"


def test_tls_sends_the_real_host_name_to_a_pinned_address() -> None:
    for mode in ("require", "verify-full"):
        context = pinned_tls("ep-x.aws.neon.tech", mode)
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2
        # asyncpg gives the IP address as the host name; the context sends the real name.
        bio = context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="203.0.113.5")
        assert bio.server_hostname == "ep-x.aws.neon.tech"
    full = pinned_tls("db.example.com", "verify-full")
    assert (full.verify_mode, full.check_hostname) == (ssl.CERT_REQUIRED, True)
    assert pinned_tls("db.example.com", "require").verify_mode == ssl.CERT_NONE


def test_the_default_timeout_is_30_seconds() -> None:
    assert TIMEOUT_S == 30.0
    assert DatabaseGateway().timeout_s == 30.0


def rows(count: int, width: int = 10) -> _Rows:
    shown: list[str] = []
    used = 0
    for i in range(count):
        line = json.dumps([i, "x" * width], separators=(",", ":"))
        if len(shown) >= MAX_ROWS or used + len(line) + 1 > MAX_MODEL_BYTES:
            break
        shown.append(line)
        used += len(line) + 1
    return _Rows(["id", "name"], 0, shown, count, True, "csv", "")


@pytest.mark.parametrize(("count", "width"), [(5_000, 10), (400, 400), (2_000, 30)])
def test_the_model_text_stays_under_the_tool_output_cap(count: int, width: int) -> None:
    text = _render(rows(count, width), path="/w/.sama/spill/x.csv", conn="pg", write=False)
    wrapped = untrusted(text, source="connector:pg")
    assert len(wrapped.encode()) < MAX_OUTPUT_BYTES
    assert "/w/.sama/spill/x.csv" in text
    assert len(text.splitlines()) <= MAX_ROWS + 4


def test_the_execute_arguments_fit_on_the_approval_card() -> None:
    # The card shows MAX_ARGUMENT_CHARS of the arguments; the gateway refuses a longer call.
    assert MAX_CARD_CHARS < MAX_ARGUMENT_CHARS
    longest = {"sql": "x" * MAX_EXECUTE_SQL_CHARS, "params": ["y" * 10] * 20}
    _check_card(longest)
    with pytest.raises(DriverError, match="approval card"):
        _check_card({"sql": "x" * MAX_EXECUTE_SQL_CHARS, "params": ["y" * 100] * 20})


async def test_the_gateway_connects_only_to_public_addresses() -> None:
    async def resolve(host: str, port: int) -> list[Any]:
        import ipaddress

        return [ipaddress.ip_address("10.0.0.5")]

    gateway = DatabaseGateway(resolver=resolve)
    for host in ("db.internal.example", "127.0.0.1", "169.254.169.254", "localhost"):
        with pytest.raises(DriverError, match="public"):
            await gateway.test(manifest(), {**VALUES, "host": host})


def test_the_module_keeps_the_limits() -> None:
    assert database.MAX_ROWS == 1_000
    assert database.MAX_MODEL_BYTES + 1_500 < MAX_OUTPUT_BYTES


def test_database_execute_always_asks() -> None:
    assert always_asks("db_postgres_execute")
    assert always_asks("mcp__sama__db_my_neon_execute")
    assert always_asks("repo_push_approval")
    for name in ("db_postgres_query", "db_postgres_export", "gmail_send_email", "execute_code"):
        assert not always_asks(name), name
    allow = (
        PolicyRule(level=RuleLevel.USER, tool="db_postgres_execute", action="allow"),
        PolicyRule(level=RuleLevel.ORG, tool="db_*", action="allow"),
    )
    verdict = policy_verdict(allow, "db_postgres_execute")
    assert verdict.action == "ask"
    deny = (*allow, PolicyRule(level=RuleLevel.ORG, tool="db_*", action="deny"))
    assert policy_verdict(deny, "db_postgres_execute").action == "deny"


async def test_a_workflow_tool_step_cannot_call_a_database_tool() -> None:
    """A database tool saves files in the session of its run. A workflow tool step has no
    run, so ``call_as`` without claims refuses it before it reads a connection."""
    from sama.connectors.service import ConnectionService, ConnectorError, RunScope

    catalog = Catalog.load(ROOT / "connectors" / "catalog")
    service = ConnectionService(cast(Any, None), catalog)
    scope = RunScope(org_id=uuid.uuid4(), workspace_id=uuid.uuid4(), user_id=None, shared=False)
    with pytest.raises(ConnectorError, match="Use an agent step"):
        await service.call_as(scope, "db_postgres_query", {"sql": "select 1"})
    await service.aclose()
