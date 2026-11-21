# sama end-to-end tests

A Playwright suite (TypeScript) that tests the main flows of sama on a deployed system:
staging by default. It drives the web app in Chromium, and it uses the API for flows that have
no page yet and to check results. See roadmap item M7.11 and architecture Section 19.6.

The suite runs every night and on demand in the workflow `End-to-end (staging)`
(`.github/workflows/e2e.yml`). It is not a check of pull requests. The CI job "Web" only lints
and typechecks the suite and runs the unit tests of its helpers.

## Run it

```sh
cd e2e
npm ci
npx playwright install chromium
export E2E_SUPABASE_SERVICE_ROLE_KEY=...   # the service_role key of the staging Supabase project
npx playwright test                        # all flows
npx playwright test tests/02-chat.spec.ts  # one file
npx playwright show-report                 # the HTML report
```

| Variable | Needed | What it is |
| --- | --- | --- |
| `E2E_SUPABASE_SERVICE_ROLE_KEY` | yes | Makes and removes the test users. Never give it to a test page. |
| `E2E_WEB_URL` | no | The web app. Default `https://sama-web-staging.vercel.app`. |
| `E2E_API_URL` | no | The API. Default `https://sama-staging.fly.dev`. |
| `E2E_SUPABASE_URL` | no | The Supabase project of the service key. Default the staging project. The run stops when `GET /config` of the API names another project, so the key never goes to another server. |
| `E2E_RUN_ID` | no | The id of the run in the test emails and org names. Default: the time and 4 random characters. |
| `E2E_WORKERS` | no | Parallel workers. Default 1, because the free plan allows 2 runs at the same time. |
| `E2E_KEEP_DATA` | no | `1` keeps the test org and users after the run, for a look at a failure. |
| `E2E_REUSE_WORLD` | no | `1` uses the kept test org and users again, so the setup does not make new ones. For work on a test, with `E2E_KEEP_DATA=1`. |
| `E2E_SWEEP_MIN_AGE_HOURS` | no | The sweep removes test users older than this. Default 3. |

## Test data

- The global setup makes two users with confirmed emails, `sama-e2e+<run>-owner@example.com`
  and `sama-e2e+<run>-member@example.com`, with random passwords. The owner makes the test
  org `e2e-<run>-main` with self-serve sign-up (free plan). No email is sent.
- The world file `.state/world.json` (mode 0600, ignored by git) keeps the users and the org
  for the tests.
- The global teardown deletes each test org with the API (all its rows go, Section 17.4) and
  then deletes the users in Supabase. It tries an org delete again after a `409` or a server
  error. When an org does not go, its owner and the world file stay for the sweep.
- `npm run sweep` removes what a stopped run left: each user with the `sama-e2e+` prefix that
  is older than 3 hours, and the `e2e-` orgs that the user owns. The nightly workflow runs it
  first. `npm run sweep -- --dry-run` only lists them.

The staging Supabase project uses the built-in mailer, which sends only to the team. So the
sign-up test sends the browser's `/auth/v1/signup` call to the admin API (the same user, no
email) and confirms the address with the admin API, as the link in the email would.

## Flows

| File | Flows (roadmap items) |
| --- | --- |
| `00-smoke` | API health and config; each page of the shell opens, also Admin and the setup guide; the agent page (M3.8, M3.12, M6.9). |
| `01-auth` | Wrong password; sign-up, email confirmation, sign-in, first org, the setup guide with a first message and its answer, sign-out (M3.8, M6.5, M6.9). |
| `02-chat` | Chat with a streamed answer on the Claude runtime; run traces; export as Markdown and JSON; session search; chat on the OpenAI runtime after a switch on the agent page; an OpenAI-runtime chat that calls the slow `browser_open` tool (M1, M2.5, M3.6, M3.12, PR 77). |
| `03-approvals-steer` | Approve and deny a `Bash` call; Stop (M1.13, M3.2, M3.9). |
| `04-memory` | Edit the user profile, the agent uses it in a new session, history and revert (M2.1, M3.12). |
| `05-skills` | Make, edit, version, archive, and restore a skill; built-in skills are read-only (M2.2, M3.12). |
| `06-members` | Invite link from the Members page, join from `/invite`, member view, admin routes refused (M6.5). |
| `07-jobs` | Schedule preview, new job, Run now, the fire and its output (M4.1 to M4.3). |
| `08-triggers-workflows` | Signed webhook starts a run, bad signature `401`, replay is a duplicate; a workflow runs to the end (M4.4, M4.5). |
| `09-connections` | Add the MCP everything server as a hosted stdio connection, the agent calls its `echo` tool, remove it (M3.4, M3.5, M4.11). |
| `10-browser` | The agent opens a page in the cloud browser; the `browser_task` sub-agent reaches a goal (M5.1, M5.6). |
| `11-billing-data` | Billing page and a Stripe Checkout in test mode (no payment); the export API of the org and of a user (M6.6, M6.8). |
| `12-admin-help` | Each admin page for the owner; download the org export and my data; a member gets no admin area; help pages without sign-in (M6.8, M6.9). |
| `13-m7-skills-channels` | The builtin skill library (at least 68 skills) and `/decision-memo` loads its skill; WhatsApp (Meta, Twilio, Gupshup) and email connects refuse fake credentials and leave no row; pairing codes, webhooks, and admin rules of the channels; the Settings link `?connect=whatsapp&provider=twilio` opens the form and shows the refusal (M7.7, M7.8). |
| `14-console` | Each of the 13 console sections opens; a webhook is made, gets a signed test, is edited, and is deleted; the harness sets the reasoning effort (M7.10). |
| `15-coding-agents` | Direct mode with `/claude` until `/sama`; `/codex <task>`; the approval card of `coding_agent_run` when the agent calls it (M7.2). |
| `16-busy-modes` | A message during a run joins it (note queued, then delivered or requeued); two joined messages add to the first request, and the answer has all three words (PR 92); `/bg` and the Run in parallel button start a second run at the same time; `/stop 2` stops one of two runs (M7.1). |
| `17-api-server` | An admin makes an API key on Admin > API keys; the official OpenAI Node SDK chats through `/v1` (also a stream that continues the session); the MCP TypeScript SDK lists the tools and sends a message through `/mcp`; a revoked key gets `401`; a member cannot make keys (M7.6). |
| `18-subagents` | A personal sub-agent made in the console, its test run, added to the agent on the agent page, and `delegate_task(agent=...)` checked in the trace; a member cannot write an org sub-agent (M7.4). |
| `19-custom-tools` | On the Tools page: an HTTP request tool (public echo API) and a Python script tool, each with its test button and a chat that calls it through `tool_search` (checked in the trace); a member's org tool only proposes a version (M7.3). |
| `20-hermes-parity` | `moa`, `x_search`, and `video_analyze` in a chat (checked in the trace); a cheaper model for delegated tasks and the Mem0 memory provider set on the agent page, with `memory_recall` finding a fact of an earlier session; the settings of a skill for the workspace (M7.5). |

Add a flow when its item lands on main. The staging web app deploys to Vercel by hand
(`web/README.md`), not from CI. A flow of a new page fails while that deploy is behind `main`,
so deploy the web app first.

A staging deploy replaces the API machine. A test that runs during the switch can get a `503`.
The nightly run starts at 03:00 IST, when deploys are rare, and CI retries a failed test once. Keep each test independent of the order of the files,
except inside a file with `test.describe.configure({ mode: "serial" })`.

## Rules for tests

- Wait for a condition, never for a time (`page.waitForTimeout` fails the lint).
- Find elements by role, label, or text, as a person does. Do not use CSS classes, except
  `.alert-error` for the error alerts of the app.
- Put a unique word (`marker()`) in each prompt and wait for it in the answer. Do not check the
  exact words of a model.
- For a tool flow, also check the run trace (`lib/trace.ts`): the tool ran and had no error.
- Do not print tokens, passwords, or keys. Errors of the HTTP helper drop the query string.
- A test that makes data that the teardown does not know removes it in a `finally` block.
