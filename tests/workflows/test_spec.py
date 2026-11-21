"""The workflow definition (docs/architecture.md, Section 12.3): YAML parse, checks, templates,
conditions, and wait times. These tests use no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sama.workflows import spec
from sama.workflows.spec import WorkflowError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

ACCESS = {"notes_read": "read", "notes_write": "write", "notes_delete": "destructive"}


def access_of(name: str) -> str | None:
    return ACCESS.get(name)


def parse(source: str) -> dict[str, Any]:
    return spec.parse(source, access_of=access_of, now=NOW)


def refused(source: str) -> str:
    """The reason that the parse gives for a bad definition."""
    with pytest.raises(WorkflowError) as error:
        parse(source)
    return str(error.value)


def one_step(step: str, *, header: str = "name: flow") -> str:
    """The YAML of a workflow with the given steps text, indented as a list item."""
    return f"{header}\nsteps:\n{step}\n"


# -- YAML -------------------------------------------------------------------------------------


def test_load_yaml_returns_a_plain_mapping() -> None:
    data = spec.load_yaml("name: a\nsteps: []\n1: x\n")
    assert data == {"name": "a", "steps": [], "1": "x"}


def test_load_yaml_turns_dates_into_iso_text() -> None:
    data = spec.load_yaml("when: 2026-03-04\nat: 2026-03-04 05:06:07\n")
    assert data == {"when": "2026-03-04", "at": "2026-03-04T05:06:07"}


def test_load_yaml_refuses_bad_yaml_with_the_line() -> None:
    with pytest.raises(WorkflowError, match=r"not valid at line 2"):
        spec.load_yaml("name: a\n  bad: [unclosed\n")


def test_load_yaml_refuses_a_text_that_is_not_a_mapping() -> None:
    with pytest.raises(WorkflowError, match="must be a mapping"):
        spec.load_yaml("- a\n- b\n")
    with pytest.raises(WorkflowError, match="must be a mapping"):
        spec.load_yaml("")


def test_load_yaml_refuses_aliases() -> None:
    with pytest.raises(WorkflowError, match="aliases"):
        spec.load_yaml("a: &x [1, 2]\nb: *x\n")


def test_load_yaml_refuses_a_big_text() -> None:
    with pytest.raises(WorkflowError, match="longer than 50 KB"):
        spec.load_yaml("a: " + "x" * spec.MAX_YAML_BYTES)


def test_load_yaml_refuses_nan() -> None:
    with pytest.raises(WorkflowError, match="NaN"):
        spec.load_yaml("a: .nan\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a: .inf\n", "infinity"),
        ("a: -.inf\n", "infinity"),
        ("a: !!binary aGVsbG8=\n", "JSON cannot hold: bytes"),
        ("a: !!set { x }\n", "JSON cannot hold: set"),
        ("a: [1, !!binary aGVsbG8=]\n", "JSON cannot hold: bytes"),
    ],
)
def test_load_yaml_refuses_values_that_json_cannot_hold(text: str, message: str) -> None:
    """A value that JSON cannot hold is a user error, not a crash of ``definition_hash``."""
    with pytest.raises(WorkflowError, match=message):
        spec.load_yaml(text)


def test_load_yaml_does_not_build_python_objects() -> None:
    with pytest.raises(WorkflowError, match="not valid"):
        spec.load_yaml("a: !!python/object/apply:os.getcwd []\n")


# -- the top level ----------------------------------------------------------------------------


def test_parse_a_minimal_definition() -> None:
    definition = parse("name: flow\nsteps:\n  - id: a\n    tool: notes_read\n")
    assert definition == {
        "name": "flow",
        "description": "",
        "inputs": {},
        "triggers": [],
        "steps": [{"id": "a", "kind": "tool", "tool": "notes_read", "args": {}}],
    }


def test_parse_keeps_the_description_trimmed() -> None:
    definition = parse(
        "name: flow\ndescription: '  A note.  '\nsteps:\n  - id: a\n    tool: notes_read\n"
    )
    assert definition["description"] == "A note."


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("steps: []\n", "needs name"),
        ("name: flow\n", "needs steps"),
        ("name: flow\nsteps: []\nextra: 1\n", "unknown keys: extra"),
        ("name: 'bad name'\nsteps: []\n", "name must be 1 to 100"),
        ("name: ''\nsteps: []\n", "name must be 1 to 100"),
        ("name: 5\nsteps: []\n", "name must be 1 to 100"),
        ("name: flow\nsteps: []\n", "at least one step"),
        ("name: flow\nsteps: nope\n", "steps must be a list"),
        ("name: flow\ndescription: [a]\nsteps: []\n", "description must be a text"),
    ],
)
def test_parse_refuses_a_bad_top_level(source: str, message: str) -> None:
    assert message in refused(source)


def test_parse_refuses_a_long_description() -> None:
    text = "x" * (spec.MAX_DESCRIPTION_CHARS + 1)
    source = f"name: flow\ndescription: {text}\nsteps:\n  - id: a\n    tool: notes_read\n"
    assert "description must be a text" in refused(source)


def test_parse_refuses_more_than_the_step_limit() -> None:
    lines = "".join(f"  - id: s{i}\n    tool: notes_read\n" for i in range(spec.MAX_STEPS + 1))
    assert f"at most {spec.MAX_STEPS} steps" in refused(f"name: flow\nsteps:\n{lines}")


def test_parse_counts_nested_steps_in_the_limit() -> None:
    inner = "".join(f"          - id: s{i}\n            tool: notes_read\n" for i in range(50))
    source = "name: flow\nsteps:\n  - id: p\n    parallel:\n      branches:\n        one:\n" + inner
    assert f"at most {spec.MAX_STEPS} steps" in refused(source)


# -- inputs -----------------------------------------------------------------------------------


def test_inputs_are_required_when_they_have_no_default() -> None:
    definition = parse(
        "name: flow\n"
        "inputs:\n"
        "  who:\n"
        "  city: { default: Oslo }\n"
        "  zip: { default: null, required: true }\n"
        "  note: { description: Free text, required: false }\n"
        "steps:\n  - id: a\n    tool: notes_read\n"
    )
    inputs = definition["inputs"]
    assert inputs["who"] == {"default": None, "required": True, "description": ""}
    assert inputs["city"] == {"default": "Oslo", "required": False, "description": ""}
    assert inputs["zip"]["required"] is True
    assert inputs["note"] == {"default": None, "required": False, "description": "Free text"}


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ("inputs: [a]", "inputs must be a mapping"),
        ("inputs: { 'bad name': {} }", "input name 'bad name' is not valid"),
        ("inputs: { a: { oops: 1 } }", "input a has unknown keys: oops"),
        ("inputs: { a: { required: maybe } }", "required must be true or false"),
        ("inputs: { a: { description: [x] } }", "description must be a short text"),
        ("inputs: { a: [1] }", "input a must be a mapping"),
    ],
)
def test_parse_refuses_bad_inputs(inputs: str, message: str) -> None:
    source = f"name: flow\n{inputs}\nsteps:\n  - id: a\n    tool: notes_read\n"
    assert message in refused(source)


def test_parse_refuses_too_many_inputs() -> None:
    names = ", ".join(f"i{n}: {{ default: 1 }}" for n in range(spec.MAX_INPUTS + 1))
    source = f"name: flow\ninputs: {{ {names} }}\nsteps:\n  - id: a\n    tool: notes_read\n"
    assert f"at most {spec.MAX_INPUTS} inputs" in refused(source)


def definition_with_inputs() -> dict[str, Any]:
    return parse(
        "name: flow\n"
        "inputs:\n"
        "  who: {}\n"
        "  city: { default: Oslo }\n"
        "steps:\n  - id: a\n    tool: notes_read\n"
    )


def test_resolve_inputs_fills_defaults() -> None:
    values = spec.resolve_inputs(definition_with_inputs(), {"who": "Ana"})
    assert values == {"who": "Ana", "city": "Oslo"}


def test_resolve_inputs_prefers_a_given_value() -> None:
    values = spec.resolve_inputs(definition_with_inputs(), {"who": "Ana", "city": "Rome"})
    assert values["city"] == "Rome"


def test_resolve_inputs_refuses_an_unknown_input() -> None:
    with pytest.raises(WorkflowError, match="unknown inputs: extra, zed"):
        spec.resolve_inputs(definition_with_inputs(), {"who": "a", "zed": 1, "extra": 2})


def test_resolve_inputs_refuses_a_missing_required_input() -> None:
    with pytest.raises(WorkflowError, match="the input who is required"):
        spec.resolve_inputs(definition_with_inputs(), None)


def test_resolve_inputs_refuses_big_inputs() -> None:
    big = {"who": "x" * spec.MAX_INPUTS_BYTES}
    with pytest.raises(WorkflowError, match="larger than 20 KB"):
        spec.resolve_inputs(definition_with_inputs(), big)


def test_resolve_inputs_returns_json_values() -> None:
    values = spec.resolve_inputs(
        definition_with_inputs(), {"who": datetime(2026, 1, 2, tzinfo=UTC), "city": {"a": [1]}}
    )
    assert values == {"who": "2026-01-02 00:00:00+00:00", "city": {"a": [1]}}


def test_resolve_inputs_of_a_definition_with_no_inputs() -> None:
    definition = parse("name: flow\nsteps:\n  - id: a\n    tool: notes_read\n")
    assert spec.resolve_inputs(definition, None) == {}
    with pytest.raises(WorkflowError, match="unknown inputs"):
        spec.resolve_inputs(definition, {"a": 1})


# -- triggers ---------------------------------------------------------------------------------


def test_triggers_accept_cron_and_a_schedule_text() -> None:
    definition = parse(
        "name: flow\n"
        "triggers:\n"
        "  - cron: '0 9 * * 1-5'\n"
        "    tz: Europe/Oslo\n"
        "  - schedule: every 2 hours\n"
        "steps:\n  - id: a\n    tool: notes_read\n"
    )
    first, second = definition["triggers"]
    assert first["kind"] == "cron"
    assert first["expr"] == "0 9 * * 1-5"
    assert first["tz"] == "Europe/Oslo"
    assert second == {"kind": "interval", "every_s": 7200}


@pytest.mark.parametrize(
    ("trigger", "message"),
    [
        ("- {}", "trigger 1 needs cron or schedule, and not both"),
        ("- { cron: '0 9 * * *', schedule: hourly }", "not both"),
        ("- nope", "trigger 1 needs cron or schedule"),
        ("- { cron: '0 9 * * *', oops: 1 }", "unknown keys: oops"),
        ("- { cron: 5 }", "cron must be a text"),
        ("- { schedule: 5 }", "schedule must be a text"),
        ("- { cron: '0 9 * * *', tz: 5 }", "tz must be a time zone name"),
        ("- { cron: 'not a cron' }", "trigger 1:"),
        ("- { schedule: 'whenever' }", "trigger 1:"),
        ("- { cron: '0 9 * * *', tz: Nowhere/City }", "trigger 1:"),
    ],
)
def test_parse_refuses_bad_triggers(trigger: str, message: str) -> None:
    source = f"name: flow\ntriggers:\n  {trigger}\nsteps:\n  - id: a\n    tool: notes_read\n"
    assert message in refused(source)


def test_parse_refuses_triggers_that_are_not_a_list() -> None:
    source = "name: flow\ntriggers: {}\nsteps:\n  - id: a\n    tool: notes_read\n"
    # An empty mapping is false, so the parse treats it as no triggers.
    assert parse(source)["triggers"] == []
    source = "name: flow\ntriggers: { cron: x }\nsteps:\n  - id: a\n    tool: notes_read\n"
    assert "triggers must be a list" in refused(source)


def test_parse_refuses_too_many_triggers() -> None:
    items = "".join("  - { schedule: hourly }\n" for _ in range(spec.MAX_TRIGGERS + 1))
    source = f"name: flow\ntriggers:\n{items}steps:\n  - id: a\n    tool: notes_read\n"
    assert f"at most {spec.MAX_TRIGGERS} triggers" in refused(source)


def test_first_fire_is_the_earliest_trigger() -> None:
    definition = parse(
        "name: flow\n"
        "triggers:\n  - schedule: every 2 hours\n  - schedule: every 30 minutes\n"
        "steps:\n  - id: a\n    tool: notes_read\n"
    )
    fire = spec.first_fire(definition, NOW)
    assert fire is not None
    assert NOW < fire <= NOW + timedelta(minutes=30)


def test_first_fire_is_none_without_triggers() -> None:
    definition = parse("name: flow\nsteps:\n  - id: a\n    tool: notes_read\n")
    assert spec.first_fire(definition, NOW) is None


# -- step: tool -------------------------------------------------------------------------------


def test_tool_step_keeps_its_args() -> None:
    definition = parse(
        one_step(
            "  - id: a\n    tool: notes_write\n    args: { text: hi, n: 2 }",
        )
    )
    assert definition["steps"] == [
        {"id": "a", "kind": "tool", "tool": "notes_write", "args": {"text": "hi", "n": 2}}
    ]


@pytest.mark.parametrize(
    ("step", "message"),
    [
        ("  - id: a\n    tool: unknown_tool", "unknown_tool is not a connector tool"),
        ("  - id: a\n    tool: Bad-Name", "tool must be a tool name"),
        ("  - id: a\n    tool: notes_read\n    args: [1]", "args must be a mapping"),
        ("  - id: a\n    tool: notes_read\n    oops: 1", "unknown keys: oops"),
        ("  - id: a\n    args: {}", "exactly one of"),
        ("  - id: a\n    tool: notes_read\n    wait: { seconds: 5 }", "exactly one of"),
        ("  - id: A\n    tool: notes_read", "needs an id"),
        ("  - tool: notes_read", "needs an id"),
        ("  - notes_read", "must be a step mapping"),
        ("  - id: a\n    tool: notes_read\n  - id: a\n    tool: notes_read", "used twice"),
    ],
)
def test_parse_refuses_a_bad_tool_step(step: str, message: str) -> None:
    assert message in refused(one_step(step))


# -- step: agent ------------------------------------------------------------------------------


def test_agent_step_with_a_text_body() -> None:
    definition = parse(one_step("  - id: a\n    agent: Summarize the notes."))
    assert definition["steps"][0] == {
        "id": "a",
        "kind": "agent",
        "prompt": "Summarize the notes.",
        "profile": None,
        "model": None,
        "skills": [],
        "preapproved_tools": [],
    }


def test_agent_step_with_options() -> None:
    definition = parse(
        one_step(
            "  - id: a\n"
            "    agent:\n"
            "      prompt: Go.\n"
            "      profile: helper\n"
            "      model: ' claude-test '\n"
            "      skills: [alpha, alpha, beta]\n"
            "      preapproved_tools:\n"
            "        - tool: mcp__notes__write\n"
            "          limits: { folder: [a, b] }\n"
        )
    )
    step = definition["steps"][0]
    assert step["profile"] == "helper"
    assert step["model"] == "claude-test"
    assert step["skills"] == ["alpha", "beta"]
    assert step["preapproved_tools"] == [
        {"tool": "mcp__notes__write", "connection": "notes", "limits": {"folder": ["a", "b"]}}
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("agent: ''", "prompt must be a text"),
        ("agent: { prompt: ' ' }", "prompt must be a text"),
        ("agent: { oops: 1 }", "needs prompt"),
        ("agent: { prompt: go, oops: 1 }", "unknown keys: oops"),
        ("agent: { prompt: go, profile: 5 }", "profile must be a profile name"),
        ("agent: { prompt: go, profile: '' }", "profile must be a profile name"),
        ("agent: { prompt: go, skills: x }", "skills must be a list"),
        ("agent: { prompt: go, skills: [1] }", "skills must be a list"),
        ("agent: { prompt: go, skills: ['bad skill!'] }", "skill name"),
        ("agent: { prompt: go, model: 5 }", "model must be a model name"),
        ("agent: { prompt: go, preapproved_tools: x }", "preapproved_tools must be a list"),
        ("agent: { prompt: go, preapproved_tools: [{ oops: 1 }] }", "a pre-approval needs"),
        (
            "agent: { prompt: go, preapproved_tools: [{ tool: workflow_run }] }",
            "cannot pre-approve workflow_run",
        ),
        (
            "agent: { prompt: go, preapproved_tools: [{ tool: mcp__notes__w, connection: x }] }",
            "the connection of mcp__notes__w is notes, not x",
        ),
    ],
)
def test_parse_refuses_a_bad_agent_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


def test_parse_refuses_a_long_prompt() -> None:
    prompt = "x" * (spec.MAX_PROMPT_CHARS + 1)
    assert "prompt must be a text" in refused(one_step(f"  - id: a\n    agent: {prompt}"))


def test_parse_refuses_too_many_preapprovals() -> None:
    items = ", ".join("{ tool: mcp__notes__w }" for _ in range(spec.MAX_PREAPPROVALS + 1))
    body = f"agent: {{ prompt: go, preapproved_tools: [{items}] }}"
    assert "preapproved_tools must be a list of at most" in refused(
        one_step(f"  - id: a\n    {body}")
    )


# -- step: approval ---------------------------------------------------------------------------


def test_approval_step() -> None:
    definition = parse(one_step("  - id: ok\n    approval: { prompt: 'Send it?' }"))
    assert definition["steps"][0] == {"id": "ok", "kind": "approval", "prompt": "Send it?"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("approval: {}", "needs prompt"),
        ("approval: { prompt: '' }", "prompt must be a text"),
        ("approval: { prompt: x, oops: 1 }", "unknown keys: oops"),
        ("approval: yes", "must be a mapping"),
    ],
)
def test_parse_refuses_a_bad_approval_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


# -- step: branch -----------------------------------------------------------------------------

BRANCH = """\
  - id: look
    tool: notes_read
  - id: pick
    branch:
      when: { value: "{{steps.look.output.kind}}", equals: big }
      then:
        - id: on-big
          tool: notes_read
      else:
        - id: on-small
          tool: notes_read
"""


def test_branch_step() -> None:
    definition = parse(one_step(BRANCH))
    branch = definition["steps"][1]
    assert branch["kind"] == "branch"
    assert branch["when"] == {
        "value": "{{steps.look.output.kind}}",
        "op": "equals",
        "operand": "big",
    }
    assert [step["id"] for step in branch["then"]] == ["on-big"]
    assert [step["id"] for step in branch["else"]] == ["on-small"]


def test_branch_else_is_optional() -> None:
    definition = parse(
        one_step(
            "  - id: pick\n    branch:\n      when: { value: x, empty: false }\n"
            "      then: [{ id: on-big, tool: notes_read }]"
        )
    )
    assert definition["steps"][0]["else"] == []
    assert definition["steps"][0]["when"]["op"] == "empty"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("branch: { then: [] }", "needs when"),
        ("branch: { when: { value: x, equals: y } }", "needs then"),
        ("branch: { when: x, then: [] }", "when must be a mapping"),
        ("branch: { when: { value: x }, then: [] }", "when needs exactly one of"),
        (
            "branch: { when: { value: x, equals: y, contains: z }, then: [] }",
            "exactly one of",
        ),
        ("branch: { when: { equals: y }, then: [] }", "when needs value"),
        ("branch: { when: { value: x, empty: yes_ }, then: [] }", "empty must be true or false"),
        ("branch: { when: { value: x, equals: y }, then: [] }", "then needs at least one step"),
        (
            "branch: { when: { value: x, equals: y }, then: [{ id: t, tool: notes_read }], z: 1 }",
            "unknown keys: z",
        ),
    ],
)
def test_parse_refuses_a_bad_branch_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


def test_a_branch_step_cannot_see_the_other_arm() -> None:
    source = one_step(
        "  - id: pick\n"
        "    branch:\n"
        "      when: { value: x, equals: y }\n"
        "      then: [{ id: t, tool: notes_read }]\n"
        "      else:\n"
        "        - id: e\n"
        "          tool: notes_write\n"
        "          args: { v: '{{steps.t.output}}' }\n"
    )
    assert "step t does not end before this step" in refused(source)


# -- step: parallel ---------------------------------------------------------------------------

PARALLEL = """\
  - id: both
    parallel:
      branches:
        left:
          - id: l1
            tool: notes_read
          - id: l2
            tool: notes_read
            args: { v: "{{steps.l1.output}}" }
        right:
          - id: r1
            tool: notes_read
  - id: after
    tool: notes_read
    args: { a: "{{steps.l2.output}}", b: "{{steps.r1.output}}" }
"""


def test_parallel_step() -> None:
    definition = parse(one_step(PARALLEL))
    parallel = definition["steps"][0]
    assert parallel["kind"] == "parallel"
    assert list(parallel["branches"]) == ["left", "right"]
    assert [step["id"] for step in parallel["branches"]["left"]] == ["l1", "l2"]


def test_a_parallel_branch_cannot_see_a_sibling_branch() -> None:
    source = one_step(
        "  - id: both\n"
        "    parallel:\n"
        "      branches:\n"
        "        left: [{ id: l1, tool: notes_read }]\n"
        "        right:\n"
        "          - id: r1\n"
        "            tool: notes_read\n"
        "            args: { v: '{{steps.l1.output}}' }\n"
    )
    assert "step l1 does not end before this step" in refused(source)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("parallel: {}", "needs branches"),
        ("parallel: { branches: {} }", "branches must map 1 to 10 names"),
        ("parallel: { branches: [a] }", "branches must map"),
        ("parallel: { branches: { 'Bad Name': [{ id: x, tool: notes_read }] } }", "branch name"),
        ("parallel: { branches: { one: [] } }", "branch one needs at least one step"),
        ("parallel: { branches: { one: x } }", "must be a list of steps"),
    ],
)
def test_parse_refuses_a_bad_parallel_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


def test_parse_refuses_too_many_parallel_branches() -> None:
    names = ", ".join(
        f"b{n}: [{{ id: s{n}, tool: notes_read }}]" for n in range(spec.MAX_PARALLEL_BRANCHES + 1)
    )
    assert "branches must map 1 to 10 names" in refused(
        one_step(f"  - id: a\n    parallel: {{ branches: {{ {names} }} }}")
    )


# -- step: wait -------------------------------------------------------------------------------


def test_wait_step_kinds() -> None:
    definition = parse(
        one_step(
            "  - id: w1\n    wait: { seconds: 60 }\n"
            "  - id: w2\n    wait: { until: '2027-01-01T00:00:00Z' }\n"
            "  - id: w3\n    wait: { event: approved, timeout_seconds: 90 }\n"
            "  - id: w4\n    wait: { event: approved }\n"
        )
    )
    w1, w2, w3, w4 = definition["steps"]
    assert w1 == {"id": "w1", "kind": "wait", "seconds": 60}
    assert w2 == {"id": "w2", "kind": "wait", "until": "2027-01-01T00:00:00Z"}
    assert w3 == {"id": "w3", "kind": "wait", "event": "approved", "timeout_seconds": 90}
    assert w4["timeout_seconds"] == spec.MAX_WAIT_S


def test_wait_until_may_be_a_template() -> None:
    definition = parse(
        one_step(
            "  - id: w\n    wait: { until: '{{inputs.when}}' }",
            header="name: f\ninputs: { when: {} }",
        )
    )
    assert definition["steps"][0]["until"] == "{{inputs.when}}"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("wait: {}", "wait needs seconds, until, or event"),
        ("wait: 5", "wait must be a mapping"),
        ("wait: { seconds: 0 }", "seconds must be 1 to"),
        ("wait: { seconds: true }", "seconds must be 1 to"),
        ("wait: { seconds: 1.5 }", "seconds must be 1 to"),
        (f"wait: {{ seconds: {spec.MAX_WAIT_S + 1} }}", "seconds must be 1 to"),
        ("wait: { seconds: 5, until: x }", "unknown keys: until"),
        ("wait: { until: 5 }", "until must be an ISO time"),
        ("wait: { until: tomorrow }", "until must be an ISO time"),
        ("wait: { event: 'bad name' }", "event must be a short name"),
        ("wait: { event: 5 }", "event must be a short name"),
        ("wait: { event: go, timeout_seconds: 0 }", "timeout_seconds must be 1 to"),
        ("wait: { event: go, oops: 1 }", "unknown keys: oops"),
    ],
)
def test_parse_refuses_a_bad_wait_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


# -- step: workflow ---------------------------------------------------------------------------


def test_workflow_step_forms() -> None:
    definition = parse(
        one_step(
            "  - id: a\n    workflow: child\n"
            "  - id: b\n    workflow: { name: child, inputs: { x: '{{steps.a.output}}' } }\n"
        )
    )
    assert definition["steps"][0] == {"id": "a", "kind": "workflow", "name": "child", "inputs": {}}
    assert definition["steps"][1]["inputs"] == {"x": "{{steps.a.output}}"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("workflow: {}", "needs name"),
        ("workflow: 'bad name'", "name must be a workflow name"),
        ("workflow: { name: c, inputs: [1] }", "inputs must be a mapping"),
        ("workflow: { name: c, oops: 1 }", "unknown keys: oops"),
        ("workflow: { name: c, inputs: { x: '{{steps.zz.output}}' } }", "step zz does not end"),
    ],
)
def test_parse_refuses_a_bad_workflow_step(body: str, message: str) -> None:
    assert message in refused(one_step(f"  - id: a\n    {body}"))


# -- templates --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "message"),
    [
        ("{{ inputs.missing }}", "the input missing is not defined"),
        ("{{ steps.later.output }}", "step later does not end before this step"),
        ("{{ steps.a.output }}", "step a does not end before this step"),
        ("{{ nope }}", "is not valid"),
        ("{{ inputs.a.b }}", "is not valid"),
        ("{{ steps.look.result }}", "is not valid"),
        ("{{ steps.look.output.a.b.c.d.e.f }}", "is not valid"),
        ("{{ }}", "is not valid"),
    ],
)
def test_parse_refuses_a_bad_template(template: str, message: str) -> None:
    source = one_step(
        "  - id: look\n    tool: notes_read\n"
        f"  - id: a\n    tool: notes_write\n    args: {{ v: '{template}' }}\n"
        "  - id: later\n    tool: notes_read"
    )
    assert message in refused(source)


def test_parse_accepts_a_template_with_a_path_and_spaces() -> None:
    source = one_step(
        "  - id: look\n    tool: notes_read\n"
        "  - id: a\n    agent: 'Use {{ steps.look.output.items.first }} for {{inputs.who}}.'",
        header="name: f\ninputs: { who: {} }",
    )
    assert parse(source)["steps"][1]["kind"] == "agent"


def test_a_template_in_a_key_or_a_list_is_checked() -> None:
    source = one_step(
        "  - id: a\n    tool: notes_read\n    args: { '{{inputs.zz}}': [1] }",
    )
    assert "input zz is not defined" in refused(source)
    source = one_step("  - id: a\n    tool: notes_read\n    args: { v: ['{{inputs.zz}}'] }")
    assert "input zz is not defined" in refused(source)


def test_a_step_sees_the_steps_that_end_before_it() -> None:
    source = one_step(
        "  - id: a\n    tool: notes_read\n"
        "  - id: b\n    tool: notes_read\n    args: { v: '{{steps.a.output.x}}' }\n"
        "  - id: c\n    approval: { prompt: 'Sure? {{steps.b.output}}' }"
    )
    assert [step["id"] for step in parse(source)["steps"]] == ["a", "b", "c"]


def test_references_finds_templates_in_nested_values() -> None:
    value = {"a": "{{inputs.x}}", "b": ["{{ steps.s.output }} and {{inputs.y}}", 3], "{{k}}": None}
    assert sorted(spec.references(value)) == sorted(
        ["inputs.x", " steps.s.output ", "inputs.y", "k"]
    )


# -- the step tree ----------------------------------------------------------------------------


def test_walk_find_and_step_ids() -> None:
    definition = parse(one_step(BRANCH + PARALLEL.replace("l1", "m1").replace("l2", "m2")))
    steps = definition["steps"]
    assert spec.step_ids(steps) == [
        "look",
        "pick",
        "on-big",
        "on-small",
        "both",
        "m1",
        "m2",
        "r1",
        "after",
    ]
    found = spec.find(steps, "m2")
    assert found is not None
    assert found["kind"] == "tool"
    assert spec.find(steps, "nothing") is None


# -- approval need ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ("  - id: a\n    tool: notes_read", False),
        ("  - id: a\n    tool: notes_write", True),
        ("  - id: a\n    tool: notes_delete", True),
        ("  - id: a\n    agent: Think.", False),
        ("  - id: a\n    agent: { prompt: go, preapproved_tools: [{ tool: mcp__n__w }] }", True),
        ("  - id: a\n    approval: { prompt: 'Ok?' }", False),
        ("  - id: a\n    wait: { seconds: 5 }", False),
        ("  - id: a\n    workflow: child", False),
    ],
)
def test_needs_approval(step: str, expected: bool) -> None:
    assert spec.needs_approval(parse(one_step(step)), access_of) is expected


def test_needs_approval_looks_inside_branches_and_parallel_steps() -> None:
    source = one_step(
        "  - id: both\n    parallel:\n      branches:\n        one: [{ id: w, tool: notes_write }]"
    )
    assert spec.needs_approval(parse(source), access_of) is True


def test_needs_approval_treats_a_tool_that_vanished_as_a_write() -> None:
    definition = parse(one_step("  - id: a\n    tool: notes_read"))
    assert spec.needs_approval(definition, lambda name: None) is True


# -- hash -------------------------------------------------------------------------------------


def test_definition_hash_is_stable() -> None:
    first = parse(one_step("  - id: a\n    tool: notes_write\n    args: { x: 1, y: 2 }"))
    second = parse(one_step("  - id: a\n    tool: notes_write\n    args: { y: 2, x: 1 }"))
    assert spec.definition_hash(first) == spec.definition_hash(second)
    assert len(spec.definition_hash(first)) == 64


def test_definition_hash_changes_with_the_definition() -> None:
    first = parse(one_step("  - id: a\n    tool: notes_write\n    args: { x: 1 }"))
    second = parse(one_step("  - id: a\n    tool: notes_write\n    args: { x: 2 }"))
    assert spec.definition_hash(first) != spec.definition_hash(second)


def test_definition_hash_keeps_non_ascii_text() -> None:
    first = spec.definition_hash({"name": "é"})
    assert first == spec.definition_hash({"name": "é"})
    assert first != spec.definition_hash({"name": "e"})


# -- render -----------------------------------------------------------------------------------

CONTEXT: dict[str, Any] = {
    "inputs": {"who": "Ana", "count": 3, "tags": ["a", "b"], "empty": None},
    "steps": {"look": {"items": {"first": "pen", "n": 2}, "ok": True}, "plain": "text"},
}


def test_render_keeps_the_type_of_a_single_template() -> None:
    assert spec.render("{{inputs.count}}", CONTEXT) == 3
    assert spec.render("{{ inputs.tags }}", CONTEXT) == ["a", "b"]
    assert spec.render("{{steps.look.output.ok}}", CONTEXT) is True
    assert spec.render("{{steps.look.output.items}}", CONTEXT) == {"first": "pen", "n": 2}


def test_render_makes_text_of_a_template_in_a_text() -> None:
    text = spec.render("Hi {{inputs.who}}, {{inputs.count}} {{inputs.tags}}!", CONTEXT)
    assert text == 'Hi Ana, 3 ["a", "b"]!'


def test_render_walks_mappings_and_lists() -> None:
    value = {"a": ["{{inputs.who}}", 1, None], 2: "{{steps.look.output.items.first}}"}
    assert spec.render(value, CONTEXT) == {"a": ["Ana", 1, None], "2": "pen"}


def test_render_gives_none_or_empty_text_for_a_missing_value() -> None:
    assert spec.render("{{inputs.empty}}", CONTEXT) is None
    assert spec.render("{{inputs.nope}}", CONTEXT) is None
    assert spec.render("x{{inputs.nope}}y", CONTEXT) == "xy"
    assert spec.render("{{steps.nope.output.a}}", CONTEXT) is None
    assert spec.render("{{steps.plain.output.a}}", CONTEXT) is None
    assert spec.render("{{steps.plain.output}}", CONTEXT) == "text"


def test_render_refuses_a_bad_reference() -> None:
    with pytest.raises(WorkflowError, match="is not valid"):
        spec.render("{{ os.system }}", CONTEXT)


def test_render_does_not_expand_a_template_inside_a_value() -> None:
    context = {"inputs": {"who": "{{inputs.who}} {{steps.x.output}}"}, "steps": {}}
    assert spec.render("{{inputs.who}}", context) == "{{inputs.who}} {{steps.x.output}}"
    assert spec.render("a {{inputs.who}}", context) == "a {{inputs.who}} {{steps.x.output}}"


def test_render_leaves_other_values_as_they_are() -> None:
    assert spec.render(5, CONTEXT) == 5
    assert spec.render(None, CONTEXT) is None
    assert spec.render("no template", CONTEXT) == "no template"


def test_as_text() -> None:
    assert spec.as_text(None) == ""
    assert spec.as_text("a") == "a"
    assert spec.as_text(3) == "3"
    assert spec.as_text({"b": 1, "a": "é"}) == '{"a": "é", "b": 1}'


def wrap(value: str, source: str) -> str:
    return f"<{source}>{value}</>"


def test_render_prompt_marks_each_filled_value() -> None:
    text = spec.render_prompt(
        "Hi {{inputs.who}}. Item: {{ steps.look.output.items.first }}. Raw: {{steps.plain.output}}",
        CONTEXT,
        wrap,
    )
    assert text == (
        "Hi <workflow input who>Ana</>. Item: <workflow step look>pen</>. "
        "Raw: <workflow step plain>text</>"
    )


def test_render_prompt_does_not_mark_an_empty_value() -> None:
    assert spec.render_prompt("[{{inputs.empty}}][{{inputs.nope}}]", CONTEXT, wrap) == "[][]"


def test_render_prompt_marks_a_value_that_looks_like_a_template() -> None:
    context = {"inputs": {"who": "{{inputs.who}}"}, "steps": {}}
    assert (
        spec.render_prompt("{{inputs.who}}", context, wrap)
        == "<workflow input who>{{inputs.who}}</>"
    )


def test_render_prompt_refuses_a_bad_reference() -> None:
    with pytest.raises(WorkflowError, match="is not valid"):
        spec.render_prompt("{{ bad }}", CONTEXT, wrap)


# -- conditions -------------------------------------------------------------------------------


def condition(op: str, value: Any, operand: Any) -> dict[str, Any]:
    return {"value": value, "op": op, "operand": operand}


@pytest.mark.parametrize(
    ("op", "value", "operand", "expected"),
    [
        ("equals", "a", "a", True),
        ("equals", "a", "b", False),
        ("equals", "3", 3, True),
        ("equals", 3, "3", True),
        ("equals", True, "true", True),
        ("equals", {"a": 1}, {"a": 1}, True),
        ("equals", {"a": 1}, '{"a": 1}', False),
        ("equals", None, "", True),
        ("not_equals", "a", "b", True),
        ("not_equals", "a", "a", False),
        ("contains", "hello world", "lo w", True),
        ("contains", "hello", "xyz", False),
        ("contains", "hello", 5, False),
        ("contains", "a5b", 5, True),
        ("contains", [1, 2, 3], 2, True),
        ("contains", [1, 2, 3], 9, False),
        ("contains", {"k": 1}, "k", True),
        ("contains", {"k": 1}, "z", False),
        ("contains", 12, 1, False),
        ("contains", None, "x", False),
        ("empty", None, True, True),
        ("empty", "", True, True),
        ("empty", [], True, True),
        ("empty", {}, True, True),
        ("empty", "x", True, False),
        ("empty", 0, True, False),
        ("empty", "x", False, True),
        ("empty", "", False, False),
    ],
)
def test_holds(op: str, value: Any, operand: Any, expected: bool) -> None:
    assert spec.holds(condition(op, value, operand), {"inputs": {}, "steps": {}}) is expected


def test_holds_renders_templates() -> None:
    assert spec.holds(condition("equals", "{{inputs.who}}", "Ana"), CONTEXT) is True
    assert spec.holds(condition("contains", "{{inputs.tags}}", "b"), CONTEXT) is True
    assert spec.holds(condition("empty", "{{inputs.empty}}", True), CONTEXT) is True
    assert spec.holds(condition("equals", "{{inputs.count}}", "{{inputs.count}}"), CONTEXT) is True


# -- wait time --------------------------------------------------------------------------------


def test_parse_time_treats_a_naive_time_as_utc() -> None:
    assert spec.parse_time("2027-01-02T03:04:05", "step a") == datetime(
        2027, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    assert spec.parse_time(" 2027-01-02T03:04:05+02:00 ", "step a").utcoffset() == timedelta(
        hours=2
    )
    with pytest.raises(WorkflowError, match="step a: until must be an ISO time"):
        spec.parse_time("soon", "step a")


def test_wake_time_for_each_wait_kind() -> None:
    context: dict[str, Any] = {"inputs": {"when": "2026-01-02T00:00:00Z"}, "steps": {}}
    assert spec.wake_time({"id": "a", "seconds": 90}, context, NOW) == NOW + timedelta(seconds=90)
    assert spec.wake_time({"id": "a", "until": "{{inputs.when}}"}, context, NOW) == datetime(
        2026, 1, 2, tzinfo=UTC
    )
    assert spec.wake_time({"id": "a", "event": "e", "timeout_seconds": 60}, context, NOW) == (
        NOW + timedelta(seconds=60)
    )


def test_wake_time_refuses_a_bad_or_far_time() -> None:
    context: dict[str, Any] = {"inputs": {"when": 5, "far": "2030-01-01T00:00:00Z"}, "steps": {}}
    with pytest.raises(WorkflowError, match="until must be an ISO time"):
        spec.wake_time({"id": "a", "until": "{{inputs.when}}"}, context, NOW)
    with pytest.raises(WorkflowError, match="longest wait is 30 days"):
        spec.wake_time({"id": "a", "until": "{{inputs.far}}"}, context, NOW)
    with pytest.raises(WorkflowError, match="until must be an ISO time"):
        spec.wake_time({"id": "a", "until": "{{inputs.nope}}"}, context, NOW)


def test_a_step_id_that_yaml_reads_as_a_boolean_is_refused() -> None:
    assert "needs an id" in refused(one_step("  - id: yes\n    tool: notes_read"))
