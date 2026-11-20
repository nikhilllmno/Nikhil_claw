"""The SQL check of the database gateway (docs/architecture.md, Section 10.4)."""

# ruff: noqa: S608  (the tests build SQL text for the check; nothing runs it)

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


# -- review round 2: names follow PostgreSQL scopes and quoting ------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "select * from users u(id, contact)",
        "select * from users as u (id, name, contact)",
        "select contact from (select * from users) s(id, name, contact)",
        "with s(id, contact) as (select * from users) select * from s",
    ],
)
def test_column_names_after_an_alias_cannot_rename_a_hidden_column(sql: str) -> None:
    with pytest.raises(SqlRejectedError, match="column names after an alias"):
        check(sql, "query", HIDDEN)


def test_a_function_alias_can_name_its_columns_when_columns_are_hidden() -> None:
    check("select n from generate_series(1, 3) g(n)", "query", HIDDEN)


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        # A CTE is visible only in its own WITH query: the outer name is the real table.
        (
            "select * from secret where exists (with secret as (select 1) select * from secret)",
            False,
        ),
        ("with secret as (select 1) select * from secret", True),
        ("select * from (with secret as (select 1) select * from secret) s", True),
        # A CTE that is not RECURSIVE does not see itself or the CTEs after it.
        ("with secret as (select * from secret) select * from secret", False),
        ("with a as (select * from b), b as (select * from orders) select * from a", False),
        ("with b as (select * from orders), a as (select * from b) select * from a", True),
        ("with recursive r as (select 1 union all select 1 from r) select * from r", True),
        ("with x as (select 1) select * from orders where exists (select * from x)", True),
    ],
)
def test_a_cte_name_is_a_cte_only_where_it_is_visible(sql: str, allowed: bool) -> None:
    policy = policy_of(tables=["orders"])
    if allowed:
        check(sql, "query", policy)
    else:
        with pytest.raises(SqlRejectedError, match="allowed"):
            check(sql, "query", policy)


def test_an_alias_in_an_inner_query_does_not_hide_the_outer_table() -> None:
    sql = "select * from users where exists (select 1 from orders users)"
    assert "email" in check(sql, "query", HIDDEN).drop_columns
    with pytest.raises(SqlRejectedError, match="hidden"):
        check(
            "select users.email from users where exists (select 1 from orders users)",
            "query",
            HIDDEN,
        )


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        ('select * from "USERS"', False),
        ('select * from "Users"', False),
        ('select * from "users"', True),
        ("select * from USERS", True),
        ('select * from "PUBLIC".users', False),
    ],
)
def test_a_quoted_name_keeps_its_case(sql: str, allowed: bool) -> None:
    policy = policy_of(schemas=["public"], tables=["users"])
    if allowed:
        check(sql, "query", policy)
    else:
        with pytest.raises(SqlRejectedError, match="allowed"):
            check(sql, "query", policy)


@pytest.mark.parametrize(
    ("policy", "sql", "allowed"),
    [
        # The search path is a, b: "orders" can be b.orders, which is not allowed.
        (policy_of(schemas=["a", "b"], tables=["a.orders", "b.users"]), "orders", False),
        (policy_of(schemas=["a", "b"], tables=["a.orders", "b.users"]), "a.orders", True),
        (policy_of(schemas=["a", "b"], tables=["a.orders", "b.orders"]), "orders", True),
        (policy_of(schemas=["a", "b"], tables=["orders"]), "orders", True),
        (policy_of(tables=["a.orders", "b.users"]), "orders", False),
        (policy_of(tables=["public.orders"]), "orders", True),
        # Some tables have no schema: the search path is the default one.
        (policy_of(tables=["public.orders", "users"]), "orders", False),
        (policy_of(tables=["public.orders", "users"]), "public.orders", True),
    ],
)
def test_a_name_without_a_schema_must_be_allowed_in_each_schema_of_the_path(
    policy: Policy, sql: str, allowed: bool
) -> None:
    if allowed:
        check(f"select * from {sql}", "query", policy)
    else:
        with pytest.raises(SqlRejectedError, match="schema of orders"):
            check(f"select * from {sql}", "query", policy)


@pytest.mark.parametrize("mode", ["query", "execute"])
@pytest.mark.parametrize(
    "function",
    [
        "pg_stat_reset()",
        "pg_stat_reset_shared('bgwriter')",
        "pg_stat_reset_single_table_counters(1)",
        "pg_backup_start('x')",
        "pg_start_backup('x')",
        "pg_wal_replay_pause()",
        "pg_log_backend_memory_contexts(1)",
        "pg_restore_relation_stats('relation', 'x')",
        "pg_clear_relation_stats('public', 'x')",
    ],
)
def test_functions_that_change_the_server_are_rejected(mode: Mode, function: str) -> None:
    sql = f"select {function}" if mode == "query" else f"delete from t where 1 = {function}"
    with pytest.raises(SqlRejectedError, match="is not allowed"):
        check(sql, mode, OPEN)


@pytest.mark.parametrize(
    "sql",
    [
        "select x.contact from lateral (select * from users) as x(id, name, contact)",
        "select 1 from orders o join lateral (select * from users) x(id, name, contact) on true",
    ],
)
def test_a_lateral_alias_cannot_rename_a_hidden_column(sql: str) -> None:
    with pytest.raises(SqlRejectedError, match="column names after an alias"):
        check(sql, "query", HIDDEN)


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        # The inner u is orders: PostgreSQL looks in the nearest FROM first.
        (
            "select u.id from users u where exists (select 1 from orders u where u.email = 'a')",
            True,
        ),
        ("select u.email from orders u where exists (select 1 from users u)", True),
        ("select u.email from users u where exists (select 1 from orders u)", False),
        ("select o.email from orders o join users u on u.id = o.id", True),
        ("with c as (select * from users) select c.email from c", False),
        ("with users as (select * from users) select users.email from users", False),
        ("select n from lateral generate_series(1, 3) g(n)", True),
        ("select x from unnest(array[1]) t(x)", True),
        ("select a from (values (1)) v(a)", True),
    ],
)
def test_a_qualifier_is_found_in_the_nearest_from(sql: str, allowed: bool) -> None:
    if allowed:
        check(sql, "query", HIDDEN)
    else:
        with pytest.raises(SqlRejectedError, match="hidden"):
            check(sql, "query", HIDDEN)
