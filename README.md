# Nikhil_claw

A cloud agent engine with Hermes-level features, built on the Claude Agent SDK and the OpenAI Agents SDK.

Status: M1 (core engine) in progress. The runtime contract and the fake runtime are done.

## Development

You need [uv](https://docs.astral.sh/uv/). uv installs Python 3.12.

```bash
uv sync                      # install claw and the dev tools
uv run pytest                # tests
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pre-commit install    # run the checks before each commit
```

The runtime SDKs are in separate dependency groups. Install them only when you work on an
adapter:

```bash
uv sync --group runtime-claude   # claude-agent-sdk
uv sync --group runtime-openai   # openai-agents
```

Code layout: `src/claw/runtime/contract.py` is the runtime contract (architecture Section 5.1).
`src/claw/runtime/fake.py` is the scripted runtime for tests.

## Docs

- [Architecture and design](docs/architecture.md)
- [Hermes feature map](docs/feature-map.md)
- [Roadmap and backlog](docs/roadmap.md)
- [Agent and contributor conventions](AGENTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
