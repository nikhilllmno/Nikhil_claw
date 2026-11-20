"""The workflow definition: YAML parse, checks, templates, and conditions (docs/architecture.md,
Section 12.3).

``parse`` turns the YAML text into the checked definition. The definition is plain JSON, so the
database stores it as it is and ``definition_hash`` binds an owner approval to it. The engine
reads only the checked definition.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import yaml
from pydantic import ValidationError

from sama.scheduler import schedule as schedules
from sama.scheduler.spec import (
    JobError,
    PreapprovalIn,
    parse_model,
    parse_preapproval,
    parse_skills,
)

MAX_YAML_BYTES: Final = 50_000
MAX_STEPS: Final = 50
MAX_TRIGGERS: Final = 5
MAX_INPUTS: Final = 20
MAX_PARALLEL_BRANCHES: Final = 10
MAX_DEPTH: Final = 3
"""The deepest chain of sub-workflows: a top instance has depth 0, its child depth 1."""
MAX_WAIT_S: Final = 30 * 24 * 3600
MAX_PROMPT_CHARS: Final = 20_000
MAX_DESCRIPTION_CHARS: Final = 2_000
MAX_INPUTS_BYTES: Final = 20_000
MAX_PREAPPROVALS: Final = 20

STEP_KINDS: Final = ("tool", "agent", "approval", "branch", "parallel", "wait", "workflow")
CONDITION_OPS: Final = ("equals", "not_equals", "contains", "empty")
WRITE_ACCESS: Final = frozenset({"write", "destructive"})

_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,100}")
_STEP_ID: Final = re.compile(r"[a-z0-9_-]{1,64}")
_INPUT: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_EVENT: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_TOOL: Final = re.compile(r"[a-z0-9_]{1,100}")
_TEMPLATE: Final = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_REFERENCE: Final = re.compile(
    r"\s*(?:inputs\.(?P<input>[A-Za-z0-9_-]{1,64})"
    r"|steps\.(?P<step>[a-z0-9_-]{1,64})\.output(?P<path>(?:\.[A-Za-z0-9_-]{1,64}){0,5}))\s*"
)

AccessOf = Callable[[str], str | None]
"""The access of a connector tool (``read``, ``write``, ``destructive``), or ``None`` when the
catalog has no tool of that name."""


class WorkflowError(Exception):
    """The workflow operation cannot run. The message is safe to show to the user."""


class WorkflowNotFoundError(WorkflowError):
    pass


# -- YAML ------------------------------------------------------------------------------------


def load_yaml(source: str) -> dict[str, Any]:
    """The mapping of a YAML text. Refuses big texts and aliases (an alias can make a small
    text expand into a large value)."""
    if len(source.encode("utf-8")) > MAX_YAML_BYTES:
        raise WorkflowError(f"the YAML text is longer than {MAX_YAML_BYTES // 1000} KB")
    try:
        for event in yaml.parse(source, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise WorkflowError("the YAML text cannot use aliases (*name)")
        data = yaml.safe_load(source)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise WorkflowError(f"the YAML text is not valid{where}") from None
    if not isinstance(data, dict):
        raise WorkflowError("the YAML text must be a mapping with name and steps")
    plain: dict[str, Any] = _plain(data)
    return plain


def _plain(value: Any) -> Any:
    """The JSON form of a YAML value: keys become text, dates become ISO text. A value that
    JSON cannot hold (binary data, a set, NaN, or infinity) is an error for the user."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise WorkflowError("the YAML text cannot hold NaN or infinity")
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise WorkflowError(f"the YAML text has a value that JSON cannot hold: {type(value).__name__}")


# -- definition ------------------------------------------------------------------------------


def parse(source: str, *, access_of: AccessOf, now: datetime | None = None) -> dict[str, Any]:
    """The checked definition of a YAML text. Raises ``WorkflowError`` with a reason for the
    user."""
    return check(load_yaml(source), access_of=access_of, now=now)


def check(
    data: Mapping[str, Any], *, access_of: AccessOf, now: datetime | None = None
) -> dict[str, Any]:
    _keys(
        data,
        "the workflow",
        required={"name", "steps"},
        allowed={"description", "inputs", "triggers"},
    )
    name = data["name"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise WorkflowError("name must be 1 to 100 letters, digits, - or _")
    description = data.get("description") or ""
    if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_CHARS:
        raise WorkflowError(f"description must be a text of at most {MAX_DESCRIPTION_CHARS} chars")
    inputs = _inputs(data.get("inputs") or {})
    triggers = _triggers(data.get("triggers") or [], now=now or datetime.now(UTC))
    checker = _Checker(access_of=access_of, inputs=set(inputs))
    steps = checker.sequence(data["steps"], "steps", visible=frozenset())
    if not steps:
        raise WorkflowError("a workflow needs at least one step")
    return {
        "name": name,
        "description": description.strip(),
        "inputs": inputs,
        "triggers": triggers,
        "steps": steps,
    }


def definition_hash(definition: Mapping[str, Any]) -> str:
    """The SHA-256 digest of the canonical definition. An owner approval stores it."""
    text = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keys(data: Any, where: str, *, required: set[str], allowed: Iterable[str] = ()) -> None:
    if not isinstance(data, Mapping):
        raise WorkflowError(f"{where} must be a mapping")
    missing = sorted(required - set(data))
    if missing:
        raise WorkflowError(f"{where} needs {', '.join(missing)}")
    unknown = sorted(set(data) - required - set(allowed))
    if unknown:
        raise WorkflowError(f"{where} has unknown keys: {', '.join(unknown)}")


def _inputs(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping):
        raise WorkflowError("inputs must be a mapping of names")
    if len(raw) > MAX_INPUTS:
        raise WorkflowError(f"a workflow has at most {MAX_INPUTS} inputs")
    result: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if not _INPUT.fullmatch(name):
            raise WorkflowError(f"the input name {name!r} is not valid")
        spec = spec if spec is not None else {}
        _keys(spec, f"input {name}", required=set(), allowed={"default", "required", "description"})
        required = spec.get("required", "default" not in spec)
        if not isinstance(required, bool):
            raise WorkflowError(f"input {name}: required must be true or false")
        description = spec.get("description") or ""
        if not isinstance(description, str) or len(description) > 500:
            raise WorkflowError(f"input {name}: description must be a short text")
        result[name] = {
            "default": spec.get("default"),
            "required": required,
            "description": description,
        }
    return result


def _triggers(raw: Any, *, now: datetime) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise WorkflowError("triggers must be a list")
    if len(raw) > MAX_TRIGGERS:
        raise WorkflowError(f"a workflow has at most {MAX_TRIGGERS} triggers")
    result = []
    for index, item in enumerate(raw, 1):
        where = f"trigger {index}"
        if not isinstance(item, Mapping) or ("cron" in item) == ("schedule" in item):
            raise WorkflowError(f"{where} needs cron or schedule, and not both")
        _keys(item, where, required=set(), allowed={"cron", "schedule", "tz"})
        tz = item.get("tz") or schedules.DEFAULT_TZ
        if not isinstance(tz, str):
            raise WorkflowError(f"{where}: tz must be a time zone name")
        try:
            if "cron" in item:
                expr = item["cron"]
                if not isinstance(expr, str):
                    raise WorkflowError(f"{where}: cron must be a text")
                result.append(
                    schedules.validate({"kind": "cron", "expr": expr.lower(), "tz": tz}, now=now)
                )
            else:
                text = item["schedule"]
                if not isinstance(text, str):
                    raise WorkflowError(f"{where}: schedule must be a text")
                result.append(schedules.parse(text, tz=tz, now=now))
        except schedules.ScheduleError as error:
            raise WorkflowError(f"{where}: {error}") from None
    return result


class _Checker:
    """Checks the step tree. ``visible`` holds the ids of the steps that end before a step
    starts, so a template can name only those."""

    def __init__(self, *, access_of: AccessOf, inputs: set[str]) -> None:
        self.access_of = access_of
        self.inputs = inputs
        self.ids: set[str] = set()
        self.count = 0

    def sequence(self, raw: Any, where: str, *, visible: frozenset[str]) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise WorkflowError(f"{where} must be a list of steps")
        steps = []
        seen = set(visible)
        for item in raw:
            step = self.step(item, where, visible=frozenset(seen))
            steps.append(step)
            seen.update(step_ids([step]))
        return steps

    def step(self, raw: Any, where: str, *, visible: frozenset[str]) -> dict[str, Any]:
        self.count += 1
        if self.count > MAX_STEPS:
            raise WorkflowError(f"a workflow has at most {MAX_STEPS} steps in all")
        if not isinstance(raw, Mapping):
            raise WorkflowError(f"each item of {where} must be a step mapping")
        step_id = raw.get("id")
        if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
            raise WorkflowError(
                f"each step in {where} needs an id of 1 to 64 characters a-z, 0-9, - or _"
            )
        if step_id in self.ids:
            raise WorkflowError(f"the step id {step_id} is used twice")
        self.ids.add(step_id)
        kinds = [kind for kind in STEP_KINDS if kind in raw]
        if len(kinds) != 1:
            raise WorkflowError(f"step {step_id} needs exactly one of {', '.join(STEP_KINDS)}")
        kind = kinds[0]
        here = f"step {step_id}"
        body = raw[kind]
        extra = {"args"} if kind == "tool" else set()
        _keys(raw, here, required={"id", kind}, allowed=extra)
        if kind == "tool":
            return self._tool(step_id, body, raw.get("args"), visible)
        if kind == "agent":
            return self._agent(step_id, body, visible)
        if kind == "approval":
            _keys(body, here, required={"prompt"})
            prompt = self._text(body["prompt"], here, "prompt", visible)
            return {"id": step_id, "kind": "approval", "prompt": prompt}
        if kind == "branch":
            return self._branch(step_id, body, visible)
        if kind == "parallel":
            return self._parallel(step_id, body, visible)
        if kind == "wait":
            return self._wait(step_id, body, visible)
        return self._workflow(step_id, body, visible)

    def _tool(self, step_id: str, tool: Any, args: Any, visible: frozenset[str]) -> dict[str, Any]:
        if not isinstance(tool, str) or not _TOOL.fullmatch(tool):
            raise WorkflowError(f"step {step_id}: tool must be a tool name")
        access = self.access_of(tool)
        if access is None:
            raise WorkflowError(
                f"step {step_id}: {tool} is not a connector tool. In a workflow, a tool step"
                " runs a connector tool. Use an agent step for other tools."
            )
        args = args if args is not None else {}
        if not isinstance(args, Mapping):
            raise WorkflowError(f"step {step_id}: args must be a mapping")
        self._templates(args, f"step {step_id}", visible)
        return {"id": step_id, "kind": "tool", "tool": tool, "args": dict(args)}

    def _agent(self, step_id: str, body: Any, visible: frozenset[str]) -> dict[str, Any]:
        here = f"step {step_id}"
        if isinstance(body, str):
            body = {"prompt": body}
        _keys(
            body,
            here,
            required={"prompt"},
            allowed={"profile", "model", "skills", "preapproved_tools"},
        )
        prompt = self._text(body["prompt"], here, "prompt", visible)
        profile = body.get("profile")
        if profile is not None and (not isinstance(profile, str) or not 0 < len(profile) <= 100):
            raise WorkflowError(f"{here}: profile must be a profile name or id")
        skills = body.get("skills") or []
        model = body.get("model")
        tools = body.get("preapproved_tools") or []
        if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
            raise WorkflowError(f"{here}: skills must be a list of skill names")
        if model is not None and not isinstance(model, str):
            raise WorkflowError(f"{here}: model must be a model name")
        if not isinstance(tools, list) or len(tools) > MAX_PREAPPROVALS:
            raise WorkflowError(
                f"{here}: preapproved_tools must be a list of at most {MAX_PREAPPROVALS}"
            )
        try:
            preapproved = [parse_preapproval(PreapprovalIn.model_validate(item)) for item in tools]
            checked_skills = list(parse_skills(skills))
            checked_model = parse_model(model)
        except ValidationError:
            raise WorkflowError(f"{here}: a pre-approval needs tool, connection, limits") from None
        except JobError as error:
            raise WorkflowError(f"{here}: {error}") from None
        return {
            "id": step_id,
            "kind": "agent",
            "prompt": prompt,
            "profile": profile,
            "model": checked_model,
            "skills": checked_skills,
            "preapproved_tools": preapproved,
        }

    def _branch(self, step_id: str, body: Any, visible: frozenset[str]) -> dict[str, Any]:
        here = f"step {step_id}"
        _keys(body, here, required={"when", "then"}, allowed={"else"})
        when = body["when"]
        if not isinstance(when, Mapping):
            raise WorkflowError(f"{here}: when must be a mapping")
        ops = [op for op in CONDITION_OPS if op in when]
        if len(ops) != 1:
            raise WorkflowError(f"{here}: when needs exactly one of {', '.join(CONDITION_OPS)}")
        op = ops[0]
        _keys(when, f"{here} when", required={"value", op})
        operand = when[op]
        if op == "empty" and not isinstance(operand, bool):
            raise WorkflowError(f"{here}: empty must be true or false")
        self._templates(when["value"], here, visible)
        self._templates(operand, here, visible)
        then = self.sequence(body["then"], f"{here} then", visible=visible)
        otherwise = self.sequence(body.get("else") or [], f"{here} else", visible=visible)
        if not then:
            raise WorkflowError(f"{here}: then needs at least one step")
        return {
            "id": step_id,
            "kind": "branch",
            "when": {"value": when["value"], "op": op, "operand": operand},
            "then": then,
            "else": otherwise,
        }

    def _parallel(self, step_id: str, body: Any, visible: frozenset[str]) -> dict[str, Any]:
        here = f"step {step_id}"
        _keys(body, here, required={"branches"})
        branches = body["branches"]
        if not isinstance(branches, Mapping) or not 0 < len(branches) <= MAX_PARALLEL_BRANCHES:
            raise WorkflowError(f"{here}: branches must map 1 to {MAX_PARALLEL_BRANCHES} names")
        result = {}
        for name, steps in branches.items():
            if not _STEP_ID.fullmatch(name):
                raise WorkflowError(f"{here}: the branch name {name!r} is not valid")
            checked = self.sequence(steps, f"{here} branch {name}", visible=visible)
            if not checked:
                raise WorkflowError(f"{here}: branch {name} needs at least one step")
            result[name] = checked
        return {"id": step_id, "kind": "parallel", "branches": result}

    def _wait(self, step_id: str, body: Any, visible: frozenset[str]) -> dict[str, Any]:
        here = f"step {step_id}"
        if not isinstance(body, Mapping):
            raise WorkflowError(f"{here}: wait must be a mapping")
        if "seconds" in body:
            _keys(body, here, required={"seconds"})
            seconds = body["seconds"]
            if not _int_in(seconds, 1, MAX_WAIT_S):
                raise WorkflowError(f"{here}: seconds must be 1 to {MAX_WAIT_S}")
            return {"id": step_id, "kind": "wait", "seconds": seconds}
        if "until" in body:
            _keys(body, here, required={"until"})
            until = body["until"]
            if not isinstance(until, str):
                raise WorkflowError(f"{here}: until must be an ISO time")
            if not _TEMPLATE.search(until):
                parse_time(until, here)
            self._templates(until, here, visible)
            return {"id": step_id, "kind": "wait", "until": until}
        if "event" in body:
            _keys(body, here, required={"event"}, allowed={"timeout_seconds"})
            event = body["event"]
            timeout = body.get("timeout_seconds", MAX_WAIT_S)
            if not isinstance(event, str) or not _EVENT.fullmatch(event):
                raise WorkflowError(f"{here}: event must be a short name")
            if not _int_in(timeout, 1, MAX_WAIT_S):
                raise WorkflowError(f"{here}: timeout_seconds must be 1 to {MAX_WAIT_S}")
            return {"id": step_id, "kind": "wait", "event": event, "timeout_seconds": timeout}
        raise WorkflowError(f"{here}: wait needs seconds, until, or event")

    def _workflow(self, step_id: str, body: Any, visible: frozenset[str]) -> dict[str, Any]:
        here = f"step {step_id}"
        if isinstance(body, str):
            body = {"name": body}
        _keys(body, here, required={"name"}, allowed={"inputs"})
        name = body["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise WorkflowError(f"{here}: name must be a workflow name")
        inputs = body.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            raise WorkflowError(f"{here}: inputs must be a mapping")
        self._templates(inputs, here, visible)
        return {"id": step_id, "kind": "workflow", "name": name, "inputs": dict(inputs)}

    def _text(self, value: Any, here: str, field: str, visible: frozenset[str]) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_PROMPT_CHARS:
            raise WorkflowError(f"{here}: {field} must be a text of 1 to {MAX_PROMPT_CHARS} chars")
        self._templates(value, here, visible)
        return value

    def _templates(self, value: Any, here: str, visible: frozenset[str]) -> None:
        for reference in references(value):
            match = _REFERENCE.fullmatch(reference)
            if match is None:
                raise WorkflowError(
                    f"{here}: the template {{{{{reference.strip()}}}}} is not valid"
                )
            if match["input"] is not None and match["input"] not in self.inputs:
                raise WorkflowError(f"{here}: the input {match['input']} is not defined")
            if match["step"] is not None and match["step"] not in visible:
                raise WorkflowError(
                    f"{here}: the step {match['step']} does not end before this step"
                )


def _int_in(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def parse_time(text: str, here: str) -> datetime:
    """An ISO time. A time without a zone is UTC."""
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise WorkflowError(f"{here}: until must be an ISO time") from None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


# -- the step tree ---------------------------------------------------------------------------


def walk(steps: Sequence[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """Every step of a tree, in document order."""
    for step in steps:
        yield step
        if step["kind"] == "branch":
            yield from walk(step["then"])
            yield from walk(step["else"])
        elif step["kind"] == "parallel":
            for inner in step["branches"].values():
                yield from walk(inner)


def step_ids(steps: Sequence[Mapping[str, Any]]) -> list[str]:
    return [step["id"] for step in walk(steps)]


def find(steps: Sequence[Mapping[str, Any]], step_id: str) -> Mapping[str, Any] | None:
    return next((step for step in walk(steps) if step["id"] == step_id), None)


def approval_reasons(definition: Mapping[str, Any], access_of: AccessOf) -> list[str]:
    """Why a version needs the approval of its owner: a tool step writes (``write`` or
    ``destructive`` access; an unknown tool counts as ``write``), or an agent step pre-approves
    tools."""
    reasons = []
    for step in walk(definition["steps"]):
        if step["kind"] == "tool":
            access = access_of(step["tool"]) or "write"
            if access in WRITE_ACCESS:
                reasons.append(f"step {step['id']} calls {step['tool']} ({access})")
        elif step["kind"] == "agent" and step["preapproved_tools"]:
            names = ", ".join(item["tool"] for item in step["preapproved_tools"])
            reasons.append(f"step {step['id']} pre-approves tools: {names}")
    return reasons


def needs_approval(definition: Mapping[str, Any], access_of: AccessOf) -> bool:
    return bool(approval_reasons(definition, access_of))


def first_fire(
    definition: Mapping[str, Any], after: datetime, *, last: datetime | None = None
) -> datetime | None:
    """The next fire time of the triggers of a definition, or ``None``."""
    times = [
        when
        for trigger in definition.get("triggers", [])
        if (when := schedules.next_fire(trigger, after, last=last)) is not None
    ]
    return min(times, default=None)


# -- inputs and templates --------------------------------------------------------------------


def resolve_inputs(
    definition: Mapping[str, Any], given: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The input values of an instance: the given values and the defaults."""
    given = dict(given or {})
    spec = definition["inputs"]
    unknown = sorted(set(given) - set(spec))
    if unknown:
        raise WorkflowError(f"unknown inputs: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for name, item in spec.items():
        if name in given:
            values[name] = given[name]
        elif item["required"]:
            raise WorkflowError(f"the input {name} is required")
        else:
            values[name] = item["default"]
    size = len(json.dumps(values, ensure_ascii=False, default=str).encode("utf-8"))
    if size > MAX_INPUTS_BYTES:
        raise WorkflowError(f"the inputs are larger than {MAX_INPUTS_BYTES // 1000} KB")
    return json.loads(json.dumps(values, default=str))  # type: ignore[no-any-return]


def references(value: Any) -> Iterator[str]:
    """The text inside each ``{{ }}`` of a value."""
    if isinstance(value, str):
        yield from (match.group(1) for match in _TEMPLATE.finditer(value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from references(key)
            yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def render(value: Any, context: Mapping[str, Any]) -> Any:
    """The value with each template filled. ``context`` has ``inputs`` and ``steps`` (a map of
    step ids to outputs). A text that is one template keeps the JSON type of the value."""
    if isinstance(value, str):
        found = _TEMPLATE.findall(value)
        if len(found) == 1 and _TEMPLATE.fullmatch(value):
            return _lookup(found[0], context)
        return _TEMPLATE.sub(lambda match: as_text(_lookup(match.group(1), context)), value)
    if isinstance(value, Mapping):
        return {str(key): render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value


def render_prompt(text: str, context: Mapping[str, Any], wrap: Callable[[str, str], str]) -> str:
    """The prompt of an agent step with each template filled. ``wrap(value, source)`` marks
    each filled value: an input or a step output can hold data from outside (a tool answer or
    the payload of a trigger), so the model must not follow it as an instruction."""

    def one(match: re.Match[str]) -> str:
        reference = match.group(1)
        value = as_text(_lookup(reference, context))
        parsed = _REFERENCE.fullmatch(reference)
        if not value or parsed is None:
            return value
        if parsed["step"] is not None:
            return wrap(value, f"workflow step {parsed['step']}")
        return wrap(value, f"workflow input {parsed['input']}")

    return _TEMPLATE.sub(one, text)


def _lookup(reference: str, context: Mapping[str, Any]) -> Any:
    match = _REFERENCE.fullmatch(reference)
    if match is None:
        raise WorkflowError(f"the template {{{{{reference.strip()}}}}} is not valid")
    if match["input"] is not None:
        return context["inputs"].get(match["input"])
    value = context["steps"].get(match["step"])
    for key in [part for part in (match["path"] or "").split(".") if part]:
        value = value.get(key) if isinstance(value, Mapping) else None
    return value


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def holds(condition: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    """The result of a fixed comparison. Sama does not evaluate code."""
    value = render(condition["value"], context)
    operand = render(condition["operand"], context)
    op = condition["op"]
    if op == "empty":
        return (value in (None, "", [], {})) is operand
    if op in ("equals", "not_equals"):
        same = value == operand or (
            not isinstance(value, (dict, list)) and as_text(value) == as_text(operand)
        )
        return same if op == "equals" else not same
    if isinstance(value, list):
        return operand in value
    if isinstance(value, (str, Mapping)):
        return as_text(operand) in value
    return False


def wake_time(step: Mapping[str, Any], context: Mapping[str, Any], now: datetime) -> datetime:
    """The time when a wait step ends: the end of the time wait, or the time limit of an event
    wait."""
    if "seconds" in step:
        return now + timedelta(seconds=int(step["seconds"]))
    if "until" in step:
        rendered = render(step["until"], context)
        if not isinstance(rendered, str):
            raise WorkflowError(f"step {step['id']}: until must be an ISO time")
        when = parse_time(rendered, f"step {step['id']}")
        if when - now > timedelta(seconds=MAX_WAIT_S):
            raise WorkflowError(f"step {step['id']}: the longest wait is 30 days")
        return when
    return now + timedelta(seconds=int(step["timeout_seconds"]))
