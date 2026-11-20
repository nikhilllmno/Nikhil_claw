# Sama

A cloud agent engine with Hermes-level features, built on the Claude Agent SDK and the OpenAI Agents SDK.

Status: M1 (core engine) in progress. The runtime contract, the fake runtime, the Claude
Agent SDK adapter, the conformance suite, the database with row-level security, the
sandbox providers with the runner, the model proxy, and the MCP tool server are done.

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

Live tests call real services (the Claude API and the Supabase project). They cost money,
so they run only when you ask:

```bash
SAMA_LIVE_TESTS=1 uv run pytest -m live   # needs ANTHROPIC_API_KEY and the SAMA_SUPABASE_* variables
```

The runtime conformance suite (architecture Section 5.5) is in `tests/conformance/`. `uv run
pytest` runs it on the fake runtime and on the Claude CLI with a scripted model, at no cost.
`-m live` adds a real Claude model. A GitHub Actions job runs the live suite every night.

### Sandbox

The agent runs in a sandbox, one per session (architecture Section 16.2). E2B is the cloud
provider. Docker is for local development and CI. Build the sama image and run the Docker
tests:

```bash
docker build -f sandbox/Dockerfile -t sama-runner:dev .
SAMA_DOCKER_IMAGE=sama-runner:dev uv run pytest tests/sandbox tests/conformance -k docker
```

Build the E2B template `sama-runner` and run the E2B tests (needs `E2B_API_KEY`):

```bash
uv run python -m sama.sandbox.template
SAMA_LIVE_TESTS=1 uv run pytest -m live tests/sandbox
```

### Model proxy

The model proxy is the only path from a sandbox to a model provider (architecture Section
16.6). It holds the provider keys, accepts only run tokens, enforces budgets, and records the
cost of each call. Start it on the control plane:

```bash
export SAMA_RUN_TOKEN_KEY=...   # the run token key of the orchestrator, 32 bytes or more
export ANTHROPIC_API_KEY=...    # and OPENAI_API_KEY for OpenAI models
uv run python -m sama.modelproxy --port 8787
```

Set `SandboxSettings.model_url` to the proxy URL. The runner then gives the Claude CLI
`<url>/anthropic` and the run token as its key. Prices are in
`src/sama/modelproxy/prices.toml`. A live test runs a real Claude call through the proxy:

```bash
SAMA_LIVE_TESTS=1 uv run pytest -m live tests/modelproxy
```

### MCP tool server

The sama MCP tool server gives both runtimes the sama tools (architecture Section 16.7):
`web_search`, `web_extract`, `clarify`, and `todo`. It accepts only run tokens. Start it on
the control plane:

```bash
export SAMA_RUN_TOKEN_KEY=...   # the run token key of the orchestrator
export BRAVE_API_KEY=...        # optional: without it, web_search is hidden
uv run python -m sama.tools --port 8788
```

Add `McpServerConfig(name="sama", url="<url>/mcp")` to the `RunConfig`. The runner sets the
run token as the bearer. A live test runs a real search and a real page fetch:

```bash
SAMA_LIVE_TESTS=1 uv run pytest -m live tests/tools
```

### Database

Sama uses Supabase Postgres (architecture Section 17.1). The database tests need a Postgres
server. They create a new database, so the URL user must be able to create databases:

```bash
docker run -d --name sama-pg -e POSTGRES_PASSWORD=sama -p 55432:5432 pgvector/pgvector:pg17
export SAMA_TEST_DATABASE_URL=postgresql://postgres:sama@localhost:55432/postgres
uv run pytest tests/db
```

The migrations need the `vector` extension (pgvector), so use a pgvector image, as above. A
plain `postgres` image fails at migration 0014. Supabase has the extension.

Without `SAMA_TEST_DATABASE_URL` these tests are skipped. CI runs them with a Postgres service.

Migrations use Alembic. Set `SAMA_DATABASE_URL` to the database URL (for Supabase, the session
pooler URL on port 5432), then run:

```bash
uv run alembic upgrade head      # apply the migrations
uv run alembic check             # the models match the migrations
```

On a plain Postgres, run `tests/db/supabase_shim.sql` first. It adds the Supabase roles and
`auth.uid()`.

The live tenancy test uses the Supabase project. It needs `SAMA_SUPABASE_URL`,
`SAMA_SUPABASE_ANON_KEY`, `SAMA_SUPABASE_SERVICE_ROLE_KEY`, and `SAMA_DATABASE_URL`:

```bash
SAMA_LIVE_TESTS=1 uv run pytest tests/db/test_supabase_live.py
```

### Code layout

- `src/sama/runtime/contract.py`: the runtime contract (architecture Section 5.1).
- `src/sama/runtime/fake.py`: the scripted runtime for tests.
- `src/sama/runtime/claude.py`: the Claude Agent SDK adapter (architecture Section 5.3).
- `src/sama/db/`: SQLAlchemy models and engine helpers. `migrations/`: Alembic migrations.
- `src/sama/sandbox/`, `src/sama/orchestrator/`, `src/sama/runtime/runner.py`: the sandbox
  providers, the orchestrator, and the runner (architecture Section 16.2).
- `src/sama/modelproxy/`: the model proxy (architecture Section 16.6).
- `src/sama/tools/`: the sama MCP tool server and the outbound URL policy (architecture
  Sections 16.4 and 16.7).

## Docs

- [Architecture and design](docs/architecture.md)
- [Hermes feature map](docs/feature-map.md)
- [Roadmap and backlog](docs/roadmap.md)
- [Agent and contributor conventions](AGENTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
