"""Builds the sama E2B template from ``sandbox/Dockerfile`` (docs/architecture.md, Section 16.2).

Run it from the repository root, with ``E2B_API_KEY`` in the environment:

    uv run python -m sama.sandbox.template [--alias sama-runner]
    uv run python -m sama.sandbox.template --print-alias

E2B builds the image on its side. The build uploads only the files that the Dockerfile copies.

``--print-alias`` prints ``sama-runner-<hash>``. The hash covers the Dockerfile, the files
that pin the dependencies, the build limits, and the source files that the sandbox entry points
can import. A deploy that changes none of them gets the same alias, so the session sandboxes
stay (docs/architecture.md, Section 19.3).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Final

from sama.sandbox.base import DEFAULT_CPU_COUNT, DEFAULT_MEMORY_MB
from sama.sandbox.e2b import DEFAULT_TEMPLATE
from sama.warm import PRELOAD

log: Final = logging.getLogger("sama.sandbox.template")

DOCKERFILE: Final = Path("sandbox/Dockerfile")
IGNORE: Final = ["**/__pycache__", ".venv", ".git", "**/*.pyc"]

ENTRY_POINTS: Final = ("sama.runtime.runner", "sama.warm", *PRELOAD)
"""The modules that the orchestrator starts in the sandbox (``RUNNER_ARGV``,
``WARM_RUNNER_ARGV``), and the modules that the warm runner imports by name."""
DYNAMIC_IMPORT_OK: Final = frozenset({"sama.warm"})
"""Modules that can import by name. Their targets must be in ``ENTRY_POINTS``."""
BUILD_FILES: Final = (DOCKERFILE, Path("pyproject.toml"), Path("uv.lock"), Path("README.md"))
ALIAS_PREFIX: Final = "sama-runner-"
HASH_CHARS: Final = 12
DYNAMIC_IMPORTS: Final = frozenset({"import_module", "__import__"})


class FingerprintError(Exception):
    """The source has an import that the fingerprint cannot follow."""


def _module_file(src: Path, module: str) -> Path | None:
    base = src.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _package(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _imports(module: str, path: Path) -> set[str]:
    """Returns the ``sama`` modules that the file can import, also in function bodies."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = _package(module, path).split(".")
                parts = parts[: len(parts) - node.level + 1]
                base = ".".join([*parts, base] if base else parts)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in DYNAMIC_IMPORTS and module not in DYNAMIC_IMPORT_OK:
                raise FingerprintError(f"{path}: {name} is not allowed in the sandbox code")
    return {name for name in found if name == "sama" or name.startswith("sama.")}


def sandbox_sources(root: Path) -> list[Path]:
    """Returns the source files that the sandbox entry points can import, sorted.

    The search reads the import statements of each file, also the imports in function bodies.
    An import of a name that is not a module (``from sama.x import Name``) adds nothing. Each
    module adds the ``__init__.py`` files of its packages, because Python runs them first.
    """
    src = root / "src"
    seen: set[str] = set()
    files: set[Path] = set()
    todo = list(ENTRY_POINTS)
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_file(src, module)
        if path is None:
            if module in ENTRY_POINTS:
                raise FingerprintError(f"no source file for {module}")
            continue
        files.add(path)
        parts = module.split(".")
        todo.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        todo.extend(_imports(module, path))
    return sorted(files)


def fingerprint(root: Path) -> str:
    """Returns the hash of everything that goes into the template (hex, SHA-256)."""
    digest = hashlib.sha256(f"cpu={DEFAULT_CPU_COUNT};memory={DEFAULT_MEMORY_MB}\n".encode())
    for path in [*(root / name for name in BUILD_FILES), *sandbox_sources(root)]:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def content_alias(root: Path) -> str:
    """Returns ``sama-runner-<first 12 characters of the fingerprint>``."""
    return ALIAS_PREFIX + fingerprint(root)[:HASH_CHARS]


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
    parser.add_argument(
        "--print-alias", action="store_true", help="print the content alias and do not build"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if not (args.root / DOCKERFILE).is_file():
        log.error("run from the repository root, or pass --root")
        return 2
    if args.print_alias:
        print(content_alias(args.root))
        return 0
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per status poll
    api_key = os.environ.get("E2B_API_KEY")
    if not api_key:
        log.error("set E2B_API_KEY")
        return 2
    template_id = asyncio.run(build(args.root, args.alias, api_key))
    log.info("built the template alias=%s id=%s", args.alias, template_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
