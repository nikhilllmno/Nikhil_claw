"""Builds the sama E2B template from ``sandbox/Dockerfile`` (docs/architecture.md, Section 16.2).

Run it from the repository root, with ``E2B_API_KEY`` in the environment:

    uv run python -m sama.sandbox.template [--alias sama-runner]

E2B builds the image on its side. The build uploads only the files that the Dockerfile copies.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Final

from sama.sandbox.base import DEFAULT_CPU_COUNT, DEFAULT_MEMORY_MB
from sama.sandbox.e2b import DEFAULT_TEMPLATE

log: Final = logging.getLogger("sama.sandbox.template")

DOCKERFILE: Final = Path("sandbox/Dockerfile")
IGNORE: Final = ["**/__pycache__", ".venv", ".git", "**/*.pyc"]


async def build(root: Path, alias: str, api_key: str) -> str:
    """Builds the template and returns its id."""
    from e2b import AsyncTemplate, Template

    template = Template(file_context_path=root, file_ignore_patterns=IGNORE).from_dockerfile(
        str(root / DOCKERFILE)
    )
    info = await AsyncTemplate.build(
        template,
        alias=alias,
        cpu_count=DEFAULT_CPU_COUNT,
        memory_mb=DEFAULT_MEMORY_MB,
        on_build_logs=lambda entry: log.info("%s", entry),
        api_key=api_key,
    )
    return info.template_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alias", default=DEFAULT_TEMPLATE)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per status poll
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        log.error("set E2B_API_KEY")
        return 2
    if not (args.root / DOCKERFILE).is_file():
        log.error("run from the repository root, or pass --root")
        return 2
    template_id = asyncio.run(build(args.root, args.alias, api_key))
    log.info("built the template alias=%s id=%s", args.alias, template_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
