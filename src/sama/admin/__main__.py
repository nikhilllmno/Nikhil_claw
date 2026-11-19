"""Admin commands: ``python -m sama.admin bootstrap ...`` (docs/architecture.md, Section 19.4).

It uses ``SAMA_DATABASE_URL``. Run it on a control plane machine, for example with
``fly ssh console -C "python -m sama.admin bootstrap ..."``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import uuid

from sama.admin.bootstrap import DEFAULT_MODEL, BootstrapError, Bootstrapped, Owner, bootstrap
from sama.db.engine import create_engine


async def _bootstrap(args: argparse.Namespace) -> Bootstrapped:
    url = os.environ.get("SAMA_DATABASE_URL", "")
    if not url:
        raise SystemExit("set SAMA_DATABASE_URL")
    engine = create_engine(url)
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
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_bootstrap(args))
    except BootstrapError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(dataclasses.asdict(result), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
