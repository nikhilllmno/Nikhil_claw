# AGENTS.md — Nikhil_claw

Nikhil_claw ("claw") is a cloud-only, multi-user assistant engine in the style of Hermes Agent.
It uses the Claude Agent SDK (default runtime) and the OpenAI Agents SDK (second runtime).
Products such as vorflux build on top of it.

## Read first

- `docs/architecture.md`: the design and decisions D1 to D16. Follow the decisions. Change a
  decision only in a pull request that updates the doc.
- `docs/roadmap.md`: milestones and backlog items with acceptance criteria.
- `docs/feature-map.md`: every Hermes feature and the claw plan for it.

## Conventions

- **Docs first.** A change to behaviour, a public interface, or a decision updates the docs in
  the same pull request.
- **Stack.** Python 3.12, uv, FastAPI, Pydantic v2, SQLAlchemy 2 (async), Alembic, pytest,
  Ruff, mypy strict. TypeScript only for `extension/` (Chrome extension) and `web/` (web app).
- **Runtime neutrality.** Claw features are claw tools (MCP) or control-plane code. Do not put
  a feature only in one runtime adapter, unless the capability flags say so.
- **Tenancy.** Every table has `org_id`. Every query is scoped. Add an RLS policy for every
  new table.
- **No secrets in the sandbox, the repo, logs, or prompts.** Use the vault and the model
  proxy. `.env` files are ignored by git.
- **Tests.** New code has unit tests. A runtime adapter change runs the conformance suite.
- **Hermes.** Do not copy Hermes code. If you copy text such as a prompt, add the source to
  `THIRD_PARTY_NOTICES.md`.

## Writing style

Write docs and user-facing text in ASD-STE100 Simplified Technical English: short sentences,
active voice, one term for one thing.

## Git

- Work on a branch. Agent branches start with `vorflux/`.
- Open a pull request to `main`. Do not push to `main` directly.
- Use Conventional Commit messages (`feat:`, `fix:`, `docs:`, `chore:`).
