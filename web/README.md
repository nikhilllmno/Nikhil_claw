# sama web

The web app for sama: sign in, start sessions, chat with an agent while it works, approve tool calls, answer its questions, and edit an agent's settings (soul, model, tools, sub-agents).

It is a Next.js (App Router) app that runs entirely in the browser against the sama API. It has no server of its own and keeps no secrets.

## Run it locally

```sh
cd web
npm ci
NEXT_PUBLIC_SAMA_API_URL=https://sama-staging.fly.dev npm run dev
```

Open http://localhost:3000. Node 24 or newer is needed.

For the browser to talk to the API:

- The API must list the app's origin in `SAMA_WEB_ORIGINS` (for local work, `http://localhost:3000`). Without it the browser blocks every call (CORS).
- For emailed sign-in links, Supabase must allow the app's `/login` URL (for example `http://localhost:3000/login`) as a redirect URL.

## Environment

| Variable | Needed | What it is |
| --- | --- | --- |
| `NEXT_PUBLIC_SAMA_API_URL` | yes | Base URL of the sama API. It is read at build time. The Content-Security-Policy `connect-src` is built from it. |

The Supabase URL and anon key come from the API's `GET /config` at start, so they are not set here.

## Scripts

| Script | What it does |
| --- | --- |
| `npm run dev` | Dev server with hot reload. |
| `npm run build` | Production build. Run it with `NEXT_PUBLIC_SAMA_API_URL` set. |
| `npm run start` | Serve the production build. |
| `npm run lint` | ESLint. |
| `npx tsc --noEmit` | Type check. |
| `npm test` | Vitest unit tests (`lib/**/*.test.ts`). |

CI runs `npm ci`, lint, type check, tests and build in `.github/workflows/ci.yml` (job "Web").

## Deploy on Vercel

1. Import the repository and set the project's Root Directory to `web`.
2. Set `NEXT_PUBLIC_SAMA_API_URL` for each environment.
3. Add the deployed origin to the API's `SAMA_WEB_ORIGINS` and the `/login` URL to Supabase's redirect URLs.

`next.config.ts` sets a Content-Security-Policy, `Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.

## Routes

| Route | Screen |
| --- | --- |
| `/login` | Email and password, emailed sign-in link (PKCE), and a developer access-token box. |
| `/` | New session: workspace, agent, audience, title. |
| `/s/[sessionId]` | Chat with live streaming, approvals, questions, Stop, and `/compact`. The header has Runs and the export buttons (Markdown and JSON). |
| `/s/[sessionId]/runs` | The runs of the session with the token use and the cost. Each run opens its trace: steps, tool calls, and model calls. |
| `/agents/[profileId]` | Agent settings. Read-only unless you are an admin of the org. |
| `/tasks` | Task board of the current workspace: columns, new cards, status, assignee, and comments. |
| `/connections` | The tools that the agents can use: catalog, connect, test, and remove. |
| `/search` | Search the sessions that you can read, in the current workspace or in the whole org. |
| `/memory` | Memory editor: agent notes, your profile, and workspace notes, with the revision history and revert. |
| `/skills` | Skill editor: list, new skill, `SKILL.md`, files, versions, and archive. Built-in skills are read-only. |
| `/settings` | Account, the chat apps of each org (an admin connects or disconnects the Telegram bot), linked chat accounts with link codes, and the agents. |

## Layout

```
app/          routes, global CSS and design tokens (tokens.css)
components/   UI: shell, sidebar, chat, composer, cards, agent settings, providers,
              memory editor, skill editor, session search, run traces, settings,
              the shared page frame (Page.module.css, PageHeader)
lib/          framework-free logic with unit tests:
              api (client with one refresh and retry on 401), auth (Supabase, PKCE),
              sse (parser), stream (reconnecting event stream), chat (live state),
              profile (settings draft and validation), memory (entries and caps),
              skills (name and path checks), history (cost, tokens, trace steps, file
              download), connections, tasks, time, config
```

Tokens: all colours, spacing and type come from CSS custom properties in `app/tokens.css`. Light and dark follow `prefers-color-scheme`.

Auth: tokens live in `sessionStorage` (cleared when the tab closes). A 401 triggers one token refresh and one retry, then sign-out.

## Not in this version

A Slack connect form (the settings page shows only the Slack status), per-skill settings, and a skill revert (load an old version into the editor and save it). Tool calls that finished before the page was opened are not shown in the chat; only new ones stream in.
