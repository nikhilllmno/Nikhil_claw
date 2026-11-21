"""Exports per org and per user (docs/architecture.md, Section 17.4).

An export is a ZIP file:

- ``manifest.json``: the kind, the org, the user, the time, the row count of each table, and
  the columns that the export leaves out;
- ``tables/<table>.jsonl``: one JSON object for each row;
- ``files/<table>/<id>``: the bytes of each image, voice note, or other stored file. The row
  names its file in ``data_file``.

An org export has every row of every table with ``org_id``. A user export has the rows of
``USER_ROWS``: the rows of the user, the private sessions of the user and their rows, and the
messages that the user wrote. No export has a secret: ``SECRET_COLUMNS`` stay out. Search
indexes and embeddings stay out too (``DERIVED_COLUMNS``).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Final, Literal

from sqlalchemy import Column, Table, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from sama.db.models import Base

FORMAT: Final = "sama-export/1"
SECRET_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    "vault_secrets": frozenset({"wrapped_key", "ciphertext"}),
    "integrations": frozenset({"secret_hash"}),
    "pairing_codes": frozenset({"code_hash"}),
    "org_invites": frozenset({"token_hash"}),
}
"""Secret material. No export has it, also not in a hashed or encrypted form."""
DERIVED_COLUMNS: Final[Mapping[str, frozenset[str]]] = {
    "messages": frozenset({"search_vector"}),
    "message_embeddings": frozenset({"embedding"}),
}
"""Search indexes that sama makes from other columns."""
FILE_COLUMNS: Final[Mapping[str, str]] = {"attachments": "data", "generated_media": "data"}
"""The bytes that go into ``files/<table>/<id>``."""
_SPOOL_BYTES: Final = 8 * 1024 * 1024
EXCLUDED_TABLES: Final = frozenset({"data_purges"})
"""Tables that hold no data of the org: the purge outbox of sama."""

_PRIVATE: Final = (
    "session_id IN (SELECT id FROM sessions WHERE org_id = :org"
    " AND owner_user_id = :user AND audience = 'private')"
)
_JOBS: Final = "job_id IN (SELECT id FROM jobs WHERE org_id = :org AND owner_user_id = :user)"
_INSTANCES: Final = (
    "instance_id IN (SELECT id FROM workflow_instances WHERE org_id = :org"
    " AND owner_user_id = :user)"
)
USER_ROWS: Final[Mapping[str, str | None]] = {
    "orgs": "id = :org",
    "users": "id = :user",
    "memberships": "user_id = :user",
    "org_invites": (
        "invited_by_user_id = :user OR accepted_user_id = :user"
        " OR email = (SELECT lower(email) FROM users WHERE org_id = :org AND id = :user)"
    ),
    "workspaces": (
        "id IN (SELECT workspace_id FROM memberships WHERE org_id = :org AND user_id = :user)"
    ),
    "audit_log": "actor_user_id = :user",
    "agent_profiles": None,
    "profile_changes": "actor_user_id = :user",
    "vault_secrets": None,
    "data_purges": None,
    "message_embeddings": None,
    "connections": "owner_user_id = :user OR created_by_user_id = :user",
    "github_installations": "created_by_user_id = :user",
    "github_repos": "created_by_user_id = :user",
    "integrations": "created_by_user_id = :user",
    "skills": "user_id = :user",
    "skill_versions": (
        "author_user_id = :user OR skill_id IN"
        " (SELECT id FROM skills WHERE org_id = :org AND user_id = :user)"
    ),
    "channel_groups": "created_by_user_id = :user",
    "channel_identities": "user_id = :user",
    "context_files": "created_by_user_id = :user",
    "memories": "user_id = :user",
    "browser_contexts": "user_id = :user",
    "memory_revisions": (
        "author_user_id = :user OR memory_id IN"
        " (SELECT id FROM memories WHERE org_id = :org AND user_id = :user)"
    ),
    "pairing_codes": "user_id = :user",
    "sessions": "owner_user_id = :user AND audience = 'private'",
    "tool_policies": "user_id = :user OR created_by_user_id = :user",
    "worker_bots": "created_by_user_id = :user",
    "context_pins": _PRIVATE + " OR created_by_user_id = :user",
    "jobs": "owner_user_id = :user",
    "job_fires": _JOBS,
    "deliveries": _JOBS,
    "runs": _PRIVATE + " OR triggered_by_user_id = :user",
    "runtime_session_items": _PRIVATE,
    "session_bindings": _PRIVATE + " OR created_by_user_id = :user",
    "session_handoffs": _PRIVATE + " OR user_id = :user",
    "session_todos": _PRIVATE,
    "session_compactions": _PRIVATE,
    "tasks": ("owner_user_id = :user OR created_by_user_id = :user OR assignee_user_id = :user"),
    "task_comments": (
        "author_user_id = :user OR task_id IN"
        " (SELECT id FROM tasks WHERE org_id = :org AND owner_user_id = :user)"
    ),
    "approvals": _PRIVATE + " OR decided_by_user_id = :user",
    "generated_media": _PRIVATE,
    "github_push_grants": _PRIVATE,
    "messages": _PRIVATE + " OR author_user_id = :user",
    "attachments": (
        _PRIVATE + " OR message_id IN"  # noqa: S608 - constant SQL
        " (SELECT id FROM messages WHERE org_id = :org AND author_user_id = :user)"
    ),
    "model_calls": _PRIVATE + " OR user_id = :user",
    "run_events": _PRIVATE,
    "run_steers": _PRIVATE + " OR user_id = :user",
    "tool_calls": _PRIVATE,
    "review_proposals": _PRIVATE + " OR acting_user_id = :user",
    "triggers": "owner_user_id = :user",
    "trigger_events": (
        "trigger_id IN (SELECT id FROM triggers WHERE org_id = :org AND owner_user_id = :user)"
    ),
    "workflows": "owner_user_id = :user",
    "workflow_versions": (
        "workflow_id IN (SELECT id FROM workflows WHERE org_id = :org AND owner_user_id = :user)"
    ),
    "workflow_instances": "owner_user_id = :user",
    "workflow_steps": _INSTANCES,
    "workflow_receipts": _INSTANCES,
}
"""The rows of a user export, as a filter in each table of the org. None: the table has no
personal data, or the data is not for an export (secrets, the outbox, embeddings)."""

Kind = Literal["org", "user"]


@dataclass(slots=True)
class ExportFile:
    """A finished export. The caller deletes ``path`` after the download."""

    path: Path
    filename: str
    tables: dict[str, int] = field(default_factory=dict)
    files: int = 0


def excluded(table: str) -> frozenset[str]:
    """The columns of ``table`` that no export has."""
    return SECRET_COLUMNS.get(table, frozenset()) | DERIVED_COLUMNS.get(table, frozenset())


def export_tables() -> list[Table]:
    """The tables of an export, in foreign key order: ``orgs`` and each table with
    ``org_id``."""
    return [
        table
        for table in Base.metadata.sorted_tables
        if table.name not in EXCLUDED_TABLES and (table.name == "orgs" or "org_id" in table.c)
    ]


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return None
    raise TypeError(f"no JSON form for {type(value).__name__}")


def _columns(table: Table) -> list[Column[Any]]:
    gone = excluded(table.name)
    return [column for column in table.columns if column.name not in gone]


async def write_export(
    connection: AsyncConnection,
    org_id: uuid.UUID,
    *,
    kind: Kind,
    user_id: uuid.UUID | None = None,
    directory: Path | None = None,
) -> ExportFile:
    """Writes the export to a new temporary ZIP file. Each query has the org filter."""
    if kind == "user" and user_id is None:
        raise ValueError("a user export needs the user")
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - the caller deletes the file
        prefix="sama-export-", suffix=".zip", dir=directory, delete=False
    )
    handle.close()
    created = datetime.now(UTC)
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    name = f"sama-{kind}-{org_id}-{stamp}.zip"
    result = ExportFile(Path(handle.name), name)
    try:
        with zipfile.ZipFile(result.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for table in export_tables():
                where = _where(table, kind)
                if where is None:
                    continue
                count = await _write_table(
                    connection, archive, table, where, org_id, user_id, result
                )
                result.tables[table.name] = count
            manifest = {
                "format": FORMAT,
                "kind": kind,
                "org_id": str(org_id),
                "user_id": str(user_id) if user_id else None,
                "created_at": created.isoformat(),
                "tables": result.tables,
                "files": result.files,
                "excluded_columns": {
                    table.name: sorted(excluded(table.name))
                    for table in export_tables()
                    if excluded(table.name)
                },
            }
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    except BaseException:
        result.path.unlink(missing_ok=True)
        raise
    return result


def _where(table: Table, kind: Kind) -> str | None:
    if table.name == "orgs":
        return "id = :org"
    if kind == "org":
        return "org_id = :org"
    clause = USER_ROWS[table.name]
    return None if clause is None else f"org_id = :org AND ({clause})"


async def _write_table(
    connection: AsyncConnection,
    archive: zipfile.ZipFile,
    table: Table,
    where: str,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    result: ExportFile,
) -> int:
    columns = _columns(table)
    file_column = FILE_COLUMNS.get(table.name)
    params: dict[str, Any] = {"org": org_id}
    if ":user" in where:
        params["user"] = user_id
    statement = select(*columns).where(text(where)).execution_options(yield_per=500)
    count = 0
    stream = await connection.stream(statement, params)
    # ZIP takes one open entry at a time, and a row can have a file. So the lines wait in a
    # spooled file until the table ends.
    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_BYTES) as lines:
        async for row in stream:
            values = dict(row._mapping)
            if file_column is not None:
                data = values.pop(file_column, None)
                values["data_file"] = None
                if data is not None:
                    path = f"files/{table.name}/{values['id']}"
                    archive_write(archive, path, bytes(data))
                    values["data_file"] = path
                    result.files += 1
            _write_line(lines, values)
            count += 1
        lines.seek(0)
        with archive.open(f"tables/{table.name}.jsonl", "w", force_zip64=True) as out:
            shutil.copyfileobj(lines, out)
    return count


def archive_write(archive: zipfile.ZipFile, path: str, data: bytes) -> None:
    """Writes one stored file. The bytes of an image or a voice note are already packed."""
    archive.writestr(path, data, compress_type=zipfile.ZIP_STORED)


def _write_line(out: IO[bytes], values: Mapping[str, Any]) -> None:
    line = json.dumps(values, default=_json, ensure_ascii=False, separators=(",", ":"))
    out.write(line.encode("utf-8") + b"\n")
