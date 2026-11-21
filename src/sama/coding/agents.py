"""The two coding agents and how to start their CLIs (docs/architecture.md, Section 15.4).

The CLIs are in the sama sandbox image at pinned versions (``sandbox/Dockerfile``). The command
of each CLI is headless: no terminal, the task on stdin, one JSON event per line on stdout, and
no approval prompts of the CLI. The sandbox is the boundary of the CLI. The sama tool gate
judges the request before the CLI starts (Section 16.1).

The CLI gets the run token as its model key and the model proxy as its base URL. The run
token is also in ``SAMA_RUN_TOKEN`` for the git credential helper of a proxy clone (Section
15.1). The CLI gets no other credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sama.modelproxy.prices import PriceTable, Provider
from sama.runtime.contract import RuntimeKind

CLAUDE_CODE_VERSION: Final = "2.1.330"
"""The pinned version of ``@anthropic-ai/claude-code`` in ``sandbox/Dockerfile``."""
CODEX_VERSION: Final = "0.171.0"
"""The pinned version of ``@openai/codex`` in ``sandbox/Dockerfile``."""

CODING_AGENT_RUN: Final = "coding_agent_run"
"""The tool of the toolset ``coding_agents``. The gate also judges each request of the runtimes
``claude_code`` and ``codex`` as one call of this tool."""
MAX_PROMPT_CHARS: Final = 50_000
DEFAULT_TIMEOUT_S: Final = 20 * 60
MAX_TIMEOUT_S: Final = 50 * 60
"""A request runs at most this long. E2B keeps a sandbox at most 1 hour per connect (Section
16.2), and the run needs time for the clone and the pull request."""
RUN_TOKEN_ENV: Final = "SAMA_RUN_TOKEN"  # noqa: S105 -- the name of a variable
CODEX_PROVIDER: Final = "sama"
"""The name of the model provider that sama gives to Codex."""


class CodingError(Exception):
    """A request to a coding agent cannot run. The message is safe to show and has no
    secret."""


class CodingAgent(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


CODING_AGENTS: Final = tuple(CodingAgent)

_RUNTIMES: Final = {
    CodingAgent.CLAUDE_CODE: RuntimeKind.CLAUDE_CODE,
    CodingAgent.CODEX: RuntimeKind.CODEX,
}
_PROVIDERS: Final = {
    CodingAgent.CLAUDE_CODE: Provider.ANTHROPIC,
    CodingAgent.CODEX: Provider.OPENAI,
}
_LABELS: Final = {CodingAgent.CLAUDE_CODE: "Claude Code", CodingAgent.CODEX: "Codex"}

DEFAULT_MODELS: Final = {
    CodingAgent.CLAUDE_CODE: "claude-sonnet-4-6",
    CodingAgent.CODEX: "gpt-6.1-terra",
}
"""The model of a coding agent when the session model is of the other provider."""
CLAUDE_SMALL_MODEL: Final = "claude-haiku-4-5"
"""The model of the background calls of Claude Code (titles and short checks). It must have a
price, else the model proxy refuses the call."""


def runtime_of(agent: CodingAgent) -> RuntimeKind:
    return _RUNTIMES[agent]


def agent_for(runtime: RuntimeKind | str) -> CodingAgent | None:
    """The coding agent of a runtime, or ``None`` for a runtime that is not a coding agent."""
    for agent, kind in _RUNTIMES.items():
        if kind == runtime:
            return agent
    return None


def base_runtime(runtime: RuntimeKind | str) -> RuntimeKind:
    """The sama runtime of the same provider: ``claude_agent_sdk`` for ``claude_code``,
    ``openai_agents_sdk`` for ``codex``, and the runtime itself for the others. A background
    review uses it (Section 15.4)."""
    kind = RuntimeKind(runtime)
    if kind is RuntimeKind.CLAUDE_CODE:
        return RuntimeKind.CLAUDE_AGENT_SDK
    if kind is RuntimeKind.CODEX:
        return RuntimeKind.OPENAI_AGENTS_SDK
    return kind


def label(agent: CodingAgent) -> str:
    """The name of the agent for people: ``Claude Code`` or ``Codex``."""
    return _LABELS[agent]


def provider_of(agent: CodingAgent) -> Provider:
    return _PROVIDERS[agent]


def coding_model(agent: CodingAgent, model: str | None, prices: PriceTable | None = None) -> str:
    """The model for the CLI: ``model`` if it has a price for the provider of the agent, else
    the default model of the agent. A Claude session that calls Codex thus uses an OpenAI
    model."""
    table = prices or PriceTable.load()
    if model and table.lookup(provider_of(agent), model) is not None:
        return model
    return DEFAULT_MODELS[agent]


@dataclass(frozen=True, slots=True)
class CliCommand:
    """One start of a CLI: the command, its environment, and the text for its stdin."""

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    """Has the run token. ``repr`` hides it."""
    stdin: str = field(repr=False)


def _toml_string(value: str) -> str:
    """A TOML basic string. The values are URLs and names that sama makes, but the quote keeps
    a ``"`` or a ``\\`` from changing the config."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def cli_command(
    agent: CodingAgent,
    *,
    prompt: str,
    model: str,
    model_url: str,
    token: str,
    resume: str | None = None,
) -> CliCommand:
    """The headless command of ``agent``. ``model_url`` is the root URL of the model proxy as
    the sandbox sees it. ``resume`` is the CLI session (Claude Code) or thread (Codex) to
    continue. Codex keeps its threads in ``~/.codex`` (its default ``CODEX_HOME``)."""
    root = model_url.rstrip("/")
    if agent is CodingAgent.CLAUDE_CODE:
        argv = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            # The sandbox is the boundary. The sama gate judged the request before the start.
            # The sandbox user is not root, so the CLI accepts the flag.
            "--dangerously-skip-permissions",
            # WebFetch otherwise asks api.anthropic.com about each domain first.
            "--settings",
            '{"skipWebFetchPreflight": true}',
        ]
        if resume:
            argv += ["--resume", resume]
        env = {
            "ANTHROPIC_BASE_URL": f"{root}/anthropic",
            "ANTHROPIC_API_KEY": token,
            RUN_TOKEN_ENV: token,
            # Every model alias of the CLI goes to a model with a price. Without these, an
            # alias can send a model that the proxy refuses.
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": CLAUDE_SMALL_MODEL,
            "ANTHROPIC_SMALL_FAST_MODEL": CLAUDE_SMALL_MODEL,
            # No telemetry, error reports, update checks, or other calls to Anthropic hosts:
            # without the first flag the CLI calls api.anthropic.com directly.
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
            # A refused token (401) is otherwise retried 10 times over 3 minutes.
            "CLAUDE_CODE_MAX_RETRIES": "3",
        }
        return CliCommand(tuple(argv), env, prompt)
    provider = (
        f"model_providers.{CODEX_PROVIDER}="
        f"{{ name = {_toml_string('sama model proxy')}, "
        f"base_url = {_toml_string(root + '/openai/v1')}, "
        f'env_key = "{RUN_TOKEN_ENV}", wire_api = "responses", '
        "request_max_retries = 1, stream_max_retries = 1 }"
    )
    flags = [
        "--json",
        "--skip-git-repo-check",
        # The sandbox is the boundary: the Codex sandbox (bubblewrap) does not work in the E2B
        # VM. The sama gate judged the request before the start.
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "-c",
        f"model_provider={_toml_string(CODEX_PROVIDER)}",
        "-c",
        provider,
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="danger-full-access"',
        # No calls to other hosts: analytics, the plugin catalog (a git clone from GitHub),
        # feedback, and OpenTelemetry.
        "-c",
        "analytics.enabled=false",
        "-c",
        "feedback.enabled=false",
        "-c",
        "features.plugins=false",
        "-c",
        "features.remote_plugin=false",
        "-c",
        'otel.exporter="none"',
        "-c",
        'otel.metrics_exporter="none"',
        "-c",
        'otel.trace_exporter="none"',
        "-c",
        "check_for_update_on_startup=false",
    ]
    # ``codex exec resume`` takes its own options after ``resume``.
    argv = ["codex", "exec", "resume", *flags, resume] if resume else ["codex", "exec", *flags]
    argv.append("-")  # the prompt comes on stdin
    env = {RUN_TOKEN_ENV: token}
    return CliCommand(tuple(argv), env, prompt)
