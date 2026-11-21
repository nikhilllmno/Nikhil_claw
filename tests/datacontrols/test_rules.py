"""The export and delete rules cover every table, so a new table cannot leak or block."""

from __future__ import annotations

import re

from sqlalchemy import Integer, LargeBinary

from sama.datacontrols.delete import no_action_refs
from sama.datacontrols.export import (
    DERIVED_COLUMNS,
    EXCLUDED_TABLES,
    FILE_COLUMNS,
    SECRET_COLUMNS,
    USER_ROWS,
    excluded,
    export_tables,
)
from sama.db.models import Base

_SECRET_NAME = re.compile(r"(hash|secret|token|cipher|wrapped|password|key$)")
_ALLOWED_NAMES = {
    # Names that look like a secret but hold none: vault names, content hashes, and chat keys.
    "secret_ref",
    "idempotency_key",
    "external_key",
    "from_key",
    "to_key",
    "hash",
    "approved_hash",
}


def _org_tables() -> set[str]:
    return {
        table.name
        for table in Base.metadata.sorted_tables
        if table.name == "orgs" or "org_id" in table.c
    }


def test_each_table_of_an_org_has_a_user_export_rule() -> None:
    assert set(USER_ROWS) == _org_tables()


def test_the_export_has_each_table_of_an_org_but_the_purges() -> None:
    assert {table.name for table in export_tables()} == _org_tables() - EXCLUDED_TABLES


def test_each_binary_or_derived_column_has_a_rule() -> None:
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            kind = type(column.type).__name__.lower()
            binary = isinstance(column.type, LargeBinary)
            derived = kind in {"vector", "tsvector"}
            if not (binary or derived):
                continue
            ruled = FILE_COLUMNS.get(table.name) == column.name or column.name in excluded(
                table.name
            )
            assert ruled, f"{table.name}.{column.name}"


def test_each_column_with_a_secret_name_is_left_out_or_known() -> None:
    for table in export_tables():
        for column in table.columns:
            if not _SECRET_NAME.search(column.name) or column.name in _ALLOWED_NAMES:
                continue
            if isinstance(column.type, Integer):  # token counts
                continue
            assert column.name in SECRET_COLUMNS.get(table.name, set()), (
                f"{table.name}.{column.name}"
            )


def test_the_rules_name_real_columns() -> None:
    for rules in (SECRET_COLUMNS, DERIVED_COLUMNS):
        for table, columns in rules.items():
            assert columns <= set(Base.metadata.tables[table].c.keys()), table
    for table, column in FILE_COLUMNS.items():
        assert column in Base.metadata.tables[table].c


def test_the_null_refs_cover_the_keys_without_a_rule() -> None:
    refs = {(table.name, column) for table, column, _ in no_action_refs("users")}
    assert ("sessions", "owner_user_id") in refs
    assert all(table.name != "sessions" for table, _, _ in no_action_refs("sessions"))
    tasks = {(table.name, column, own) for table, column, own in no_action_refs("runs")}
    assert ("messages", "run_id", True) in tasks
