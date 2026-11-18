# Sama

A cloud agent engine with Hermes-level features, built on the Claude Agent SDK and the OpenAI Agents SDK.

Status: M1 (core engine) in progress. The runtime contract, the fake runtime, and the Claude
Agent SDK adapter are done.

## Development

You need [uv](https://docs.astral.sh/uv/). uv installs Python 3.12.

```bash
uv sync                      # install sama, the dev tools, and the Claude Agent SDK
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pre-commit install    # run the checks before each commit
```

The runtime SDKs are in separate dependency groups. `runtime-claude` (claude-agent-sdk) is a
default group. Install the OpenAI Agents SDK only when you work on its adapter:

```bash
uv sync --group runtime-openai   # openai-agents
```

Live tests call the real Claude API. They cost money, so they run only when you ask:

```bash
SAMA_LIVE_TESTS=1 uv run pytest -m live   # needs ANTHROPIC_API_KEY
```

Code layout:

- `src/sama/runtime/contract.py`: the runtime contract (architecture Section 5.1).
- `src/sama/runtime/fake.py`: the scripted runtime for tests.
- `src/sama/runtime/claude.py`: the Claude Agent SDK adapter (architecture Section 5.3).

## Docs

- [Architecture and design](docs/architecture.md)
- [Hermes feature map](docs/feature-map.md)
- [Roadmap and backlog](docs/roadmap.md)
- [Agent and contributor conventions](AGENTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
