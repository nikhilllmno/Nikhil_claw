"""Writes a trimmed connector manifest from the Composio tool schemas (docs/architecture.md,
Section 10.3).

Usage (needs ``COMPOSIO_API_KEY`` in the environment; the key is never written):

    uv run python scripts/composio_manifest.py outlook Outlook "Read ..." \
        OUTLOOK_OUTLOOK_LIST_MESSAGES=read OUTLOOK_OUTLOOK_SEND_EMAIL=write ...

The result goes to ``connectors/catalog/<name>.yaml``. Review it before a commit: the tool
names, the access of each tool, and the aliases.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

API = "https://backend.composio.dev/api/v3/tools/"
KEEP = {"type", "description", "properties", "required", "default", "items", "enum"}


def _trim(schema: Any) -> Any:
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key not in KEEP:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: _trim(prop) for name, prop in value.items()}
            elif key == "items":
                out[key] = _trim(value)
            elif key == "default" and value is None:
                continue
            else:
                out[key] = value
        return out
    return schema


def _tool(slug: str, key: str) -> dict[str, Any]:
    headers = {"x-api-key": key, "User-Agent": "sama"}
    request = urllib.request.Request(API + slug, headers=headers)  # noqa: S310 - fixed https host
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
        return dict(json.loads(response.read()))


def main(argv: list[str]) -> None:
    toolkit, title, description, *specs = argv
    key = os.environ["COMPOSIO_API_KEY"]
    name = toolkit
    tools = []
    for spec in specs:
        slug, _, access = spec.partition("=")
        data = _tool(slug, key)
        short = slug.lower().removeprefix(f"{toolkit}_").removeprefix(f"{toolkit}_")
        schema = _trim(data.get("input_parameters") or {"type": "object"})
        schema.setdefault("type", "object")
        tools.append(
            {
                "name": f"{name}_{short}",
                "slug": slug,
                "access": access,
                "description": str(data.get("description", ""))[:1000],
                "input_schema": schema,
            }
        )
    manifest = {
        "name": name,
        "title": title,
        "description": description,
        "kind": "composio",
        "scopes": ["personal", "workspace"],
        "composio": {"toolkit": toolkit},
        "tools": tools,
    }
    head = (
        f"# The {title} connector (docs/architecture.md, Section 10.3). Composio holds the\n"
        "# credential. The tool schemas are a trimmed copy of the Composio tool schemas.\n"
    )
    path = Path(__file__).resolve().parents[1] / "connectors" / "catalog" / f"{name}.yaml"
    path.write_text(head + yaml.safe_dump(manifest, sort_keys=False, width=100), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main(sys.argv[1:])
