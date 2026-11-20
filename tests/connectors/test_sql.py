"""The SQL check of the database gateway (docs/architecture.md, Section 10.4)."""

from __future__ import annotations

import pytest

from sama.connectors.sql import Mode, Policy, SqlRejectedError, check, parse_names, policy_of

OPEN = policy_of()
HIDDEN = policy_of(hidden_columns=["users.email", "ssn"])
LIMITED = policy_of(schemas=["public"], tables=["public.orders", "users"])
DDL = policy_of(ddl=True)


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("select * from users where id = $1", "select"),
        ("SELECT 1", "select"),
        ("values (1), (2)", "values"),
        ("select 1 union select 2", "union"),
        ("with c as (select 1 as a) select a from c", "select"),
        ("explain select * from users", "explain"),
        ("explain (analyze, format json) select * from users", "explain"),
        ("select pg_sleep(1)", "select"),
        ("select 1;", "select"),
    ],
)
def test_query_takes_one_read_statement(sql: str, kind: str) -> None:
    assert check(sql, "query", OPEN).kind == kind


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("select 1; select 2", "exactly one SQL statement"),
        ("select 1; delete from users", "exactly one SQL statement"),
        ("insert into t values (1)", "not INSERT"),
        ("update t set a = 1", "not UPDATE"),
        ("delete from t", "not DELETE"),
        ("merge into t using s on t.id = s.id when matched then delete", "not MERGE"),
        ("with x as (delete from users returning *) select * from x", "cannot run DELETE"),
        ("with x as (insert into t values (1) returning *) select * from x", "cannot run INSERT"),
        ("select email into newt from users", "SELECT INTO"),
        ("select * from users for update", "row locks"),
        ("explain delete from users", "not DELETE"),
        ("explain analyze insert into t values (1)", "not INSERT"),
        ("create table t (a int)", "not CREATE"),
        ("drop table t", "not DROP"),
        ("set role admin", "not SET"),
        ("begin", "query runs only"),
        ("copy t to stdout", "query runs only"),
        ("vacuum", "query runs only"),
        ("do $$ begin end $$", "query runs only"),
        ("select pg_catalog.pg_read_file('/etc/passwd')", "pg_read_file"),
        ("select \"pg_read_file\"('x')", "pg_read_file"),
        ("select query_to_xml('delete from t', true, true, '')", "query_to_xml"),
        ("select set_config('default_transaction_read_only', 'off', false)", "set_config"),
        ("select dblink('x', 'select 1')", "dblink"),
        ("select lo_import('/etc/passwd')", "lo_import"),
        ("select pg_terminate_backend(1)", "pg_terminate_backend"),
        ("select from where", "does not parse"),
        ("", "SQL"),
    ],
)
def test_query_rejects_a_write_or_a_second_statement(sql: str, reason: str) -> None:
    with pytest.raises(SqlRejectedError, match=reason):
        check(sql, "query", OPEN)


@pytest.mark.parametrize(
    "sql",
    [
        "create table t (a int)",
        "alter table t add column b int",
        "drop table t",
        "truncate t",
        "grant select on t to bob",
        "revoke select on t from bob",
        "comment on table t is 'x'",
        "create index i on t (a)",
        "with x as (create table y (a int)) insert into t select 1",
    ],
)
def test_ddl_is_denied_by_default(sql: str) -> None:
    with pytest.raises(SqlRejectedError, match="DDL is off"):
        check(sql, "execute", OPEN)


def test_ddl_runs_when_the_connection_allows_it() -> None:
    checked = check("create table t (a int)", "execute", DDL)
    assert (checked.kind, checked.ddl) == ("create", True)
    assert check("drop table t", "execute", DDL).ddl


@pytest.mark.parametrize(
    "sql",
    [
        "insert into t values (1) returning *",
        "update t set a = 1 where id = $1",
        "delete from t where id = 1",
        "with x as (select 1 as a) insert into t select a from x",
    ],
)
def test_execute_takes_one_write(sql: str) -> None:
    assert not check(sql, "execute", OPEN).ddl


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "copy t from program 'ls'",
        "vacuum",
        "call p()",
        "do $$ begin end $$",
        "set role admin",
        "delete from t; drop table t",
    ],
)
def test_execute_rejects_other_statements(sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        check(sql, "execute", DDL)


@pytest.mark.parametrize(
    ("sql", "drop"),
    [
        ("select * from users", {"email", "ssn"}),
        ("select * from (select * from users) s", {"email", "ssn"}),
        ("select s.* from (select * from users) s", {"email", "ssn"}),
        ("with c as (select * from users) select * from c", {"email", "ssn"}),
        ("select count(*) from users", {"email", "ssn"}),
        ("select o.email from orders o", {"ssn"}),
        ("select id, name from users", {"email", "ssn"}),
    ],
)
def test_hidden_columns_are_dropped_from_a_star(sql: str, drop: set[str]) -> None:
    assert check(sql, "query", HIDDEN).drop_columns == drop


@pytest.mark.parametrize(
    "sql",
    [
        "select id, email from users",
        "select u.id from users u where u.email like 'a%'",
        "select id from users order by email",
        "select (select email from users limit 1)",
        "select lower(x) from (select email as x from users) s",
        "select ssn from orders",
        "select row_to_json(u) from users u",
        "select to_jsonb(users) from users",
        "select to_jsonb(u.*) from users u",
        "select (u).email from users u",
        "select t from (select * from users) t",
        "select 1, 2, 3 union all select * from users",
        "select id from users where exists (select * from users)",
        "select z from (select * from users) s(a, b, z)",
        "with c as (select * from users) select row_to_json(c) from c",
        "select * from pg_stats",
        "select * from pg_catalog.pg_statistic",
    ],
)
def test_a_hidden_column_cannot_be_read(sql: str) -> None:
    with pytest.raises(SqlRejectedError):
        check(sql, "query", HIDDEN)


def test_a_write_cannot_read_or_copy_a_hidden_column() -> None:
    with pytest.raises(SqlRejectedError, match="email is hidden"):
        check("update users set name = 'x' where email = 'a' returning id", "execute", HIDDEN)
    with pytest.raises(SqlRejectedError):
        check("insert into t2 select * from users", "execute", HIDDEN)
    returning = check("update users set name = 'x' where id = 1 returning *", "execute", HIDDEN)
    assert returning.drop_columns == {"email", "ssn"}


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        ("select * from orders", True),
        ("select * from public.orders", True),
        ("select * from users", True),
        ("select * from generate_series(1, 3) g(n)", True),
        ("select * from secret", False),
        ("select * from other.orders", False),
        ("select * from pg_class", False),
        ("select * from pg_catalog.pg_class", False),
        ("select * from information_schema.tables", False),
        ("select * from db.public.orders", False),
    ],
)
def test_only_allowed_tables_can_be_read(sql: str, allowed: bool) -> None:
    if allowed:
        check(sql, "query", LIMITED)
    else:
        with pytest.raises(SqlRejectedError, match=r"allowed|catalog"):
            check(sql, "query", LIMITED)


def test_the_policy_parses_the_form_lists() -> None:
    assert parse_names(" Public, sales ,,") == ["public", "sales"]
    policy = policy_of(
        schemas=["public"], tables=["public.orders"], hidden_columns=["app.users.email", "ssn"]
    )
    assert isinstance(policy, Policy)
    assert policy.restricted
    assert policy.column_hidden("users", "EMAIL")
    assert policy.column_hidden("anything", "ssn")
    assert not policy.column_hidden("orders", "email")
    assert policy.search_path() == ("public",)
    assert policy_of().search_path() == ()


@pytest.mark.parametrize("mode", ["query", "execute"])
def test_a_long_or_empty_statement_is_rejected(mode: Mode) -> None:
    with pytest.raises(SqlRejectedError):
        check("   ", mode, OPEN)
