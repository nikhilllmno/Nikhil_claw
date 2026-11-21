"""The builtin skill library (docs/architecture.md, Section 9.2, M7.7).

Each folder of ``skills/`` gets its own tests: it passes the lint and the injection scan, it has
a category tag, it names only sama tools that exist and that its ``requires_toolsets`` give, it
hides when a required toolset is missing, and ``/<name>`` starts it.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

from sama.gate.decide import TOOLSETS
from sama.prompt.scan import scan
from sama.skills import commands
from sama.skills.builtin import BuiltinSkills
from sama.skills.library import SkillDoc, index
from sama.skills.lint import KNOWN_TOOLSETS, validate
from sama.skills.store import MemorySkillStore
from sama.tenancy import RunTokenClaims
from sama.vault.redact import redact
from tests.skills.helpers import actor
from tests.support import make_scope

ROOT_DIR: Final = Path(__file__).resolve().parents[2] / "skills"
NAMES: Final = sorted(p.name for p in ROOT_DIR.iterdir() if p.is_dir())
SKILLS: Final = BuiltinSkills(ROOT_DIR).all()

CATEGORIES: Final = frozenset(
    {
        "agents",
        "creative",
        "data",
        "devops",
        "email",
        "media",
        "notes",
        "productivity",
        "research",
        "social",
        "software",
        "web",
        "writing",
    }
)
"""The category tags. Each builtin skill has at least one (Section 9.2)."""

HERMES_CATEGORIES: Final = {
    "autonomous-ai-agents": "agents",
    "creative": "creative",
    "devops": "devops",
    "email": "email",
    "media": "media",
    "note-taking": "notes",
    "productivity": "productivity",
    "research": "research",
    "social-media": "social",
    "software-development": "software",
    "web": "web",
}
"""The categories of the Hermes bundled skills and the sama category that covers each one. The
Hermes category ``apple`` is out: its skills need a Mac (docs/feature-map.md, Section 3)."""

OPTIONAL_TOOLSETS: Final = frozenset({"todo", "clarify", "memory"})
"""A skill can name the tools of these toolsets without a requirement. The steps must still
work without them, so the skill stays visible when they are missing."""

TOOL_NAMES: Final = {tool: name for name, tools in TOOLSETS.items() for tool in tools}
NATIVE_TOOLS: Final = TOOLSETS["core"] | TOOLSETS["subagents"] | {"TodoWrite"}
"""Tool names of one runtime. A skill does not name them, so that it works on both runtimes."""

_CODE: Final = re.compile(r"`([^`\n]+)`")
_TOOL_LIKE: Final = re.compile(
    r"(?:web|browser|skill|skills|session|task|cronjob|workflow|connection|connections|delegate"
    r"|image|text|vision|repo|pr|tool|memory|change|sama|execute)_[a-z_]+"
)
"""The shape of a sama tool name. A name of this shape in code font must be a real tool."""
NOT_TOOLS: Final = frozenset({"sama_tools"})
"""Names of the tool shape that are not tools: the stub module of ``execute_code``."""


def _body(skill: SkillDoc) -> str:
    return skill.skill_md.split("\n---\n", 1)[1]


_CALL: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _names(body: str) -> set[str]:
    """The names in code font that can be a tool: a whole code span, or the name of a call in a
    code span (``web_search(query="x")``). A name in quotes is an argument, not a call."""
    names: set[str] = set()
    for span in _CODE.findall(body):
        names.add(span.strip())
        names.update(_CALL.findall(span))
    return names


def _named_tools(skill: SkillDoc) -> set[str]:
    return {name for name in _names(_body(skill)) if name in TOOL_NAMES}


def _tool_problems(body: str, requires: tuple[str, ...]) -> list[str]:
    """Why the tool names of a skill body break the rules of Section 9.2, if they do."""
    allowed = set(requires) | OPTIONAL_TOOLSETS
    problems: list[str] = []
    for name in sorted(_names(body)):
        if name in TOOL_NAMES and TOOL_NAMES[name] not in allowed:
            problems.append(f"{name} needs the toolset {TOOL_NAMES[name]}")
        if _TOOL_LIKE.fullmatch(name) and name not in TOOL_NAMES and name not in NOT_TOOLS:
            problems.append(f"{name} is not a sama tool")
        if name in NATIVE_TOOLS:
            problems.append(f"{name} is a tool of one runtime")
    return problems


def test_the_library_has_a_skill_for_each_folder() -> None:
    assert len(NAMES) >= 60
    assert sorted(SKILLS) == NAMES


def test_each_hermes_category_has_sama_skills() -> None:
    counts = Counter(tag for skill in SKILLS.values() for tag in skill.tags if tag in CATEGORIES)
    for hermes, category in HERMES_CATEGORIES.items():
        assert counts[category] >= 2, f"{hermes} has too few skills: {counts[category]}"


async def test_skills_list_finds_the_skills_of_a_category() -> None:
    store = MemorySkillStore(actor(toolsets=frozenset(KNOWN_TOOLSETS)), SKILLS)
    scope = make_scope()
    claims = RunTokenClaims(
        org_id=scope.org_id,
        workspace_id=scope.workspace_id,
        session_id=scope.session_id,
        run_id=scope.run_id,
        user_id=scope.user_id,
        token_id="t-1",
        expires_at=0,
    )
    for category in sorted(CATEGORIES):
        found = {skill.name for skill in await store.list(claims, category)}
        assert {s.name for s in SKILLS.values() if category in s.tags} <= found


def test_the_descriptions_are_different() -> None:
    descriptions = [skill.description.lower() for skill in SKILLS.values()]
    assert len(set(descriptions)) == len(descriptions)


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_passes_the_lint_and_the_scan(name: str) -> None:
    folder = ROOT_DIR / name
    skill_md = (folder / "SKILL.md").read_text(encoding="utf-8")
    parsed = validate(name, skill_md, dict(SKILLS[name].files))
    assert parsed.meta.name == name
    for text in (skill_md, parsed.meta.description, *SKILLS[name].files.values()):
        assert scan(text) == ()
        assert redact(text) == text


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_has_the_builtin_shape(name: str) -> None:
    skill = SKILLS[name]
    body = _body(skill)
    assert skill.scope == "builtin"
    assert re.fullmatch(r"\d+\.\d+\.\d+", skill.version)
    assert set(skill.tags) & CATEGORIES, "add a category tag"
    assert set(skill.requires_toolsets) <= KNOWN_TOOLSETS
    assert body.lstrip().startswith("# ")
    assert "\nUse this skill " in body, "say when to use the skill"
    assert body.count("\n## ") >= 2, "give the skill sections, for example steps and rules"
    assert name not in commands.RESERVED


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_names_only_tools_that_it_requires(name: str) -> None:
    skill = SKILLS[name]
    assert _tool_problems(_body(skill), skill.requires_toolsets) == []


@pytest.mark.parametrize(
    ("body", "requires", "problem"),
    [
        ("Use `web_search`.", (), "web_search needs the toolset web"),
        ('Call `web_search(query="x")`.', (), "web_search needs the toolset web"),
        ('Call `web_search_typo(query="x")`.', ("web",), "web_search_typo is not a sama tool"),
        ("Use `browser_task`.", ("browser",), "browser_task is not a sama tool"),
        ('Run `Bash("ls")`.', ("core",), "Bash is a tool of one runtime"),
        ("Use `WebFetch`.", ("core",), "WebFetch is a tool of one runtime"),
    ],
)
def test_the_tool_check_finds_each_kind_of_problem(
    body: str, requires: tuple[str, ...], problem: str
) -> None:
    assert problem in _tool_problems(body, requires)


def test_the_tool_check_allows_required_and_optional_tools() -> None:
    body = 'Use `web_search`, `todo`, and `clarify`. Then `sama_tools.call("x", **arguments)`.'
    assert _tool_problems(body, ("web",)) == []


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_hides_when_a_required_toolset_is_missing(name: str) -> None:
    skill = SKILLS[name]
    every = actor(toolsets=frozenset(KNOWN_TOOLSETS))
    assert skill in index(SKILLS.values(), every)
    for toolset in skill.requires_toolsets:
        if toolset == "skills":
            continue
        fewer = actor(toolsets=frozenset(KNOWN_TOOLSETS - {toolset}))
        assert skill not in index(SKILLS.values(), fewer)


@pytest.mark.parametrize("name", NAMES)
def test_the_skill_is_a_slash_command(name: str) -> None:
    assert commands.parse(f"/{name} please") == (name, "please")
    assert commands.parse(f"/{name.replace('-', '_')}") == (name, "")
