"""The check of a MongoDB read (docs/architecture.md, Section 10.4, "What M4.13 builds").

A MongoDB connection is read only. The model gives ``find`` (filter, projection, sort,
limit) or ``aggregate`` (a pipeline). ``read_of`` checks the arguments before the gateway
connects:

- The collection, and each collection of ``$lookup``, ``$graphLookup``, and ``$unionWith``,
  must be allowed. ``system.*`` collections are refused.
- Only stages that read are allowed. ``$out``, ``$merge``, and the stages that show the
  server (``$currentOp``, ``$listSessions``, ...) are not in the list.
- Operators that run code (``$where``, ``$function``, ``$accumulator``) are refused.
- When the connection has hidden fields, a reference to a hidden field (a key or a
  ``"$field"`` path), ``$$ROOT``, ``$$CURRENT``, and the operators that read a field by a
  computed name (``$getField``, ``$objectToArray``, ...) are refused. The gateway also drops
  hidden fields at every depth of the result.

This module does not import the driver.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sama.connectors.http_api import DriverError
from sama.connectors.sql import Policy

MAX_QUERY_LIMIT: Final = 100_000
"""The most documents of one read (the file limits of the gateway also apply)."""
DEFAULT_LIMIT: Final = 1_000
MAX_ARGUMENT_CHARS: Final = 20_000
MAX_DEPTH: Final = 30

_STAGES: Final = frozenset(
    {
        "$match",
        "$project",
        "$addFields",
        "$set",
        "$unset",
        "$group",
        "$sort",
        "$limit",
        "$skip",
        "$count",
        "$unwind",
        "$lookup",
        "$graphLookup",
        "$unionWith",
        "$facet",
        "$bucket",
        "$bucketAuto",
        "$sortByCount",
        "$replaceRoot",
        "$replaceWith",
        "$sample",
        "$densify",
        "$fill",
        "$setWindowFields",
        "$redact",
        "$geoNear",
        "$documents",
    }
)
"""The stages that only read. Each other stage is refused."""

_CODE: Final = frozenset({"$where", "$function", "$accumulator"})
_BY_NAME: Final = frozenset(
    {"$getField", "$setField", "$unsetField", "$objectToArray", "$$ROOT", "$$CURRENT", "$text"}
)
"""Ways to read a field without its name in the query. ``$text`` searches the fields of a
text index."""
_FIELD_KEYS: Final = frozenset(
    {
        "localField",
        "foreignField",
        "connectFromField",
        "connectToField",
        "field",
        "key",
        "partitionByFields",
        "includeArrayIndex",
    }
)
"""Keys whose string values are field names without ``$``."""


@dataclass(frozen=True, slots=True)
class MongoRead:
    """A checked read."""

    collection: str
    filter: Mapping[str, Any] | None = None
    projection: Mapping[str, Any] | None = None
    sort: Mapping[str, Any] | None = None
    limit: int = DEFAULT_LIMIT
    pipeline: Sequence[Mapping[str, Any]] | None = None
    hidden: frozenset[str] = frozenset()
    """The hidden field names (lower case) to drop from the result, at every depth."""


def collection_of(value: Any, policy: Policy) -> str:
    """A checked collection name."""
    if not isinstance(value, str) or not value.strip():
        raise DriverError("collection is required")
    name = value.strip()
    if len(name) > 120 or name.startswith(("system.", "$")) or "\0" in name:
        raise DriverError(f"{name[:60]} is not a collection that sama reads")
    if not policy.table_allowed(None, name):
        raise DriverError(f"{name} is not an allowed collection on this connection")
    return name


def read_of(arguments: Mapping[str, Any], policy: Policy, *, limit_max: int) -> MongoRead:
    """Checks the arguments of ``query``, ``export``, or ``sample``. Raises ``DriverError``."""
    size = len(json.dumps(dict(arguments), default=str))
    if size > MAX_ARGUMENT_CHARS:
        raise DriverError(f"the read is longer than {MAX_ARGUMENT_CHARS:,} characters")
    collection = collection_of(arguments.get("collection"), policy)
    names = {collection}
    limit = arguments.get("limit") or min(DEFAULT_LIMIT, limit_max)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= limit_max:
        raise DriverError(f"limit must be 1 to {limit_max:,}")
    parts: dict[str, Mapping[str, Any] | None] = {}
    for key in ("filter", "projection", "sort"):
        value = arguments.get(key)
        if value is not None and not isinstance(value, Mapping):
            raise DriverError(f"{key} must be an object")
        parts[key] = value
    pipeline = arguments.get("pipeline")
    if pipeline is not None:
        if any(parts.values()):
            raise DriverError("give pipeline or filter, projection, and sort, not both")
        if not isinstance(pipeline, list) or not pipeline or len(pipeline) > 50:
            raise DriverError("pipeline must be a list of 1 to 50 stages")
        _check_pipeline(pipeline, policy, names, 0)
    sort = parts["sort"]
    if sort is not None:
        for key, direction in sort.items():
            if direction not in (1, -1) and not isinstance(direction, Mapping):
                raise DriverError(f"sort {key[:60]} must be 1 or -1")
    for key in ("filter", "projection", "sort"):
        value = parts[key]
        if value is not None:
            _check_value(value, policy, names, 0)
    hidden = _hidden_for(policy, names)
    if hidden:
        _check_hidden(arguments, hidden)
    return MongoRead(
        collection=collection,
        filter=parts["filter"],
        projection=parts["projection"],
        sort=sort,
        limit=limit,
        pipeline=pipeline,
        hidden=hidden,
    )


def _hidden_for(policy: Policy, collections: set[str]) -> frozenset[str]:
    present = {c.lower() for c in collections}
    return frozenset(name for owner, name in policy.hidden if owner is None or owner in present)


def _check_pipeline(pipeline: Sequence[Any], policy: Policy, names: set[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise DriverError("the pipeline is too deep")
    for stage in pipeline:
        if not isinstance(stage, Mapping) or len(stage) != 1:
            raise DriverError("each stage must be an object with one key, for example {$match: {}}")
        [(name, body)] = stage.items()
        if name not in _STAGES:
            raise DriverError(f"the stage {str(name)[:40]} is not allowed (only stages that read)")
        if name in ("$lookup", "$graphLookup"):
            if not isinstance(body, Mapping):
                raise DriverError(f"{name} must be an object")
            if "from" in body:
                names.add(collection_of(body["from"], policy))
            elif name == "$graphLookup":
                raise DriverError("$graphLookup needs from")
            inner = body.get("pipeline")
            if inner is not None:
                if not isinstance(inner, list):
                    raise DriverError(f"the pipeline of {name} must be a list")
                _check_pipeline(inner, policy, names, depth + 1)
            rest = {k: v for k, v in body.items() if k not in ("from", "pipeline")}
            _check_value(rest, policy, names, depth + 1)
        elif name == "$unionWith":
            if isinstance(body, str):
                names.add(collection_of(body, policy))
                continue
            if not isinstance(body, Mapping):
                raise DriverError("$unionWith must be a collection name or an object")
            if "coll" in body:
                names.add(collection_of(body["coll"], policy))
            inner = body.get("pipeline")
            if inner is not None:
                if not isinstance(inner, list):
                    raise DriverError("the pipeline of $unionWith must be a list")
                _check_pipeline(inner, policy, names, depth + 1)
        elif name == "$facet":
            if not isinstance(body, Mapping):
                raise DriverError("$facet must be an object")
            for inner in body.values():
                if not isinstance(inner, list):
                    raise DriverError("each $facet output must be a pipeline")
                _check_pipeline(inner, policy, names, depth + 1)
        else:
            _check_value(body, policy, names, depth + 1)


def _check_value(value: Any, policy: Policy, names: set[str], depth: int) -> None:
    """Refuses the operators that run code, at any depth."""
    if depth > MAX_DEPTH:
        raise DriverError("the read is too deep")
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise DriverError("each key must be a string")
            if key in _CODE:
                raise DriverError(f"{key} is not allowed (it runs code on the server)")
            _check_value(inner, policy, names, depth + 1)
    elif isinstance(value, list):
        for inner in value:
            _check_value(inner, policy, names, depth + 1)


def _check_hidden(value: Any, hidden: frozenset[str]) -> None:
    """Refuses each reference to a hidden field: a key (``ssn`` or ``a.ssn``), a path
    (``"$ssn"`` or ``"$a.ssn"``), and the ways to read a field by a computed name."""
    if isinstance(value, Mapping):
        for key, inner in value.items():
            text = str(key)
            if text in _BY_NAME:
                raise DriverError(f"{text} is not allowed on a connection with hidden fields")
            if _names_hidden(text, hidden):
                raise DriverError(f"{text[:60]} is a hidden field")
            if text in _FIELD_KEYS:
                fields = inner if isinstance(inner, list) else [inner]
                for field in fields:
                    if isinstance(field, str) and _names_hidden(field, hidden):
                        raise DriverError(f"{field[:60]} is a hidden field")
            _check_hidden(inner, hidden)
    elif isinstance(value, list):
        for inner in value:
            _check_hidden(inner, hidden)
    elif isinstance(value, str):
        if value.startswith("$$ROOT") or value.startswith("$$CURRENT"):
            raise DriverError(f"{value[:20]} is not allowed on a connection with hidden fields")
        if value.startswith("$") and _names_hidden(value.lstrip("$"), hidden):
            raise DriverError(f"{value[:60]} is a hidden field")


def _names_hidden(path: str, hidden: frozenset[str]) -> bool:
    return any(part.lower() in hidden for part in path.split("."))


def drop_hidden(value: Any, hidden: frozenset[str]) -> Any:
    """The value without the hidden fields, at every depth."""
    if not hidden:
        return value
    if isinstance(value, Mapping):
        return {k: drop_hidden(v, hidden) for k, v in value.items() if str(k).lower() not in hidden}
    if isinstance(value, list):
        return [drop_hidden(v, hidden) for v in value]
    return value
