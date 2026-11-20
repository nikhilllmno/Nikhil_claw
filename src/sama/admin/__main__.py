"""Admin commands (docs/architecture.md, Sections 16.3 and 19.4):

- ``python -m sama.admin bootstrap ...``: the first org, owner, workspace, and agent profile.
- ``python -m sama.admin subagents show|set --org <id> --profile <id> [--file <path>]``: the
  native sub-agents of an agent profile (Section 13.1). ``set`` reads a JSON list of sub-agent
  specs from the file, or from stdin, and replaces the list. It applies from the next session.
- ``python -m sama.admin secret set|list|delete --org <id> [--name <name>]``: the vault
  entries of an org. ``set`` reads the value from stdin, so the value is not in the shell
  history or the process list.

It uses ``SAMA_DATABASE_URL``, and ``SAMA_VAULT_KEY`` for ``secret``. ``subagents set`` also
gives the redactor the secrets of the environment and the vault values, so the check of a spec
finds an opaque secret. It stops when the vault cannot be read (fail closed). Run it on a control
plane machine, for example with ``fly ssh console -C "python -m sama.admin bootstrap ..."``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import getpass
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from sama.admin.bootstrap import DEFAULT_MODEL, BootstrapError, Bootstrapped, Owner, bootstrap
from sama.db.engine import create_engine
from sama.db.models import VaultSecret
from sama.subagents import SubagentError, parse_subagents, profile_subagents, set_profile_subagents
from sama.vault.crypto import VaultKey
from sama.vault.redact import default_redactor, env_secrets
from sama.vault.standalone import vault_from_env
from sama.vault.store import SecretName, Vault


def _database_url() -> str:
    url = os.environ.get("SAMA_DATABASE_URL", "")
    if not url:
        raise SystemExit("set SAMA_DATABASE_URL")
    return url


async def _bootstrap(args: argparse.Namespace) -> Bootstrapped:
    engine = create_engine(_database_url())
    try:
        return await bootstrap(
            engine,
            org_slug=args.org_slug,
            org_name=args.org_name or args.org_slug,
            owner=Owner(
                email=args.email,
                display_name=args.display_name or args.email.split("@")[0],
                auth_user_id=args.auth_user_id,
            ),
            workspace_slug=args.workspace_slug,
            workspace_name=args.workspace_name,
            profile_name=args.profile_name,
            model=args.model,
        )
    finally:
        await engine.dispose()


def _read_value() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("value: ")
    return sys.stdin.read()


async def _secret(args: argparse.Namespace) -> dict[str, Any]:
    text = os.environ.get("SAMA_VAULT_KEY", "")
    if not text:
        raise SystemExit("set SAMA_VAULT_KEY")
    try:
        key = VaultKey.from_text(text)
    except ValueError as error:
        raise SystemExit(f"SAMA_VAULT_KEY: {error}") from None
    engine = create_engine(_database_url())
    vault = Vault(engine, key)
    org_id: uuid.UUID = args.org
    try:
        if args.action == "list":
            return {"org_id": str(org_id), "names": [n.value for n in await vault.names(org_id)]}
        name = SecretName(args.name)
        result: dict[str, Any] = {"org_id": str(org_id), "name": name.value}
        if args.action == "set":
            await vault.put(org_id, name, _read_value())
            return result | {"set": True}
        return result | {"deleted": await vault.delete(org_id, name)}
    finally:
        await engine.dispose()


ENV_SECRETS = (
    "SAMA_RUN_TOKEN_KEY",
    "SAMA_DATABASE_URL",
    "SAMA_VAULT_KEY",
    "SAMA_SUPABASE_JWT_SECRET",
    "SAMA_ALERT_SLACK_WEBHOOK_URL",
    "LANGFUSE_SECRET_KEY",
    "E2B_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "BRAVE_API_KEY",
)
"""The environment variables that hold a secret of the control plane. The redactor knows their
values before the sub-agent specs are checked."""

VAULT_UNREADABLE = "the vault cannot be read, so the sub-agents are not stored"


async def _know_the_secrets(engine: AsyncEngine, org_id: uuid.UUID) -> None:
    """Gives the redactor the environment secrets and every vault value, as the control plane
    does at start (Section 16.3). A spec with an opaque known secret is then not stored.

    Fails closed: if the vault key is bad, the table cannot be read, or an entry does not
    decrypt, there is no write. Without ``SAMA_VAULT_KEY`` the org must have no vault entry.
    """
    default_redactor().add(*env_secrets(*ENV_SECRETS))
    try:
        vault = vault_from_env(engine)
        if vault is None:
            async with engine.connect() as connection:
                stored = (
                    await connection.execute(
                        select(func.count()).where(VaultSecret.org_id == org_id)
                    )
                ).scalar_one()
            if stored:
                raise SystemExit("set SAMA_VAULT_KEY: the org has vault entries")
            return
        async with engine.connect() as connection:
            total = (
                await connection.execute(select(func.count()).select_from(VaultSecret))
            ).scalar_one()
        if await vault.refresh_redactor() != total:
            raise SystemExit(VAULT_UNREADABLE)
    except SystemExit:
        raise
    except Exception:
        # No detail: the text of a database error can have the connection URL.
        raise SystemExit(VAULT_UNREADABLE) from None


async def _subagents(args: argparse.Namespace, raw: Any) -> dict[str, Any]:
    engine = create_engine(_database_url())
    try:
        result: dict[str, Any] = {"org_id": str(args.org), "profile_id": str(args.profile)}
        if args.action == "set":
            await _know_the_secrets(engine, args.org)
            await set_profile_subagents(engine, args.org, args.profile, parse_subagents(raw))
        specs = await profile_subagents(engine, args.org, args.profile)
        return result | {"subagents": [spec.model_dump(mode="json") for spec in specs]}
    finally:
        await engine.dispose()


def _subagents_command(args: argparse.Namespace) -> int:
    try:
        raw: Any = None
        if args.action == "set":
            raw = json.loads(Path(args.file).read_text("utf-8") if args.file else sys.stdin.read())
        print(json.dumps(asyncio.run(_subagents(args, raw))))
    except (SubagentError, ValueError, OSError) as error:
        # ``ValueError`` has the JSON errors. The message has no spec text.
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sama.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    boot = commands.add_parser(
        "bootstrap", help="Create the first org, owner, workspace, and agent profile."
    )
    boot.add_argument("--org-slug", required=True)
    boot.add_argument("--org-name")
    boot.add_argument("--email", required=True, help="The email of the owner.")
    boot.add_argument("--display-name")
    boot.add_argument(
        "--auth-user-id",
        type=uuid.UUID,
        help="The Supabase Auth user id. Without it, the email finds the user.",
    )
    boot.add_argument("--workspace-slug", default="main")
    boot.add_argument("--workspace-name", default="Main")
    boot.add_argument("--profile-name", default="sama")
    boot.add_argument("--model", default=DEFAULT_MODEL)
    secret = commands.add_parser("secret", help="Set, list, or delete the vault entries of an org.")
    secret.add_argument("action", choices=("set", "list", "delete"))
    secret.add_argument("--org", type=uuid.UUID, required=True, help="The org id.")
    secret.add_argument("--name", choices=[name.value for name in SecretName])
    subagents = commands.add_parser(
        "subagents", help="Show or replace the native sub-agents of an agent profile."
    )
    subagents.add_argument("action", choices=("show", "set"))
    subagents.add_argument("--org", type=uuid.UUID, required=True, help="The org id.")
    subagents.add_argument("--profile", type=uuid.UUID, required=True, help="The profile id.")
    subagents.add_argument("--file", help="The JSON file of the specs. Without it, stdin.")
    args = parser.parse_args(argv)
    if args.command == "subagents":
        return _subagents_command(args)
    if args.command == "secret":
        if args.action != "list" and args.name is None:
            parser.error(f"secret {args.action} needs --name")
        try:
            print(json.dumps(asyncio.run(_secret(args))))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    try:
        result = asyncio.run(_bootstrap(args))
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(result), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
