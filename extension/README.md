# sama Chrome extension

The sama side panel for Chrome (Manifest V3, TypeScript). Roadmap item M5.4. The design is in
`docs/architecture.md` §14.2.1. The browser actions of M5.5 are a design in §14.2.2.

## What it does

- Sign in with the Supabase Auth account of the web app (email and password, or an access
  token in the advanced form).
- Open a WebSocket to the sama API (`/channels/chrome/ws`).
- Show a chat in the side panel. Each tab has its own private session.
- "Ask about this page": send the title, address, text, and selection of the tab, and an
  optional screenshot, as untrusted content.

## Build

```bash
npm ci
npm run lint
npm run typecheck
npm test
SAMA_API_URL=https://sama-staging.fly.dev npm run build   # writes dist/
```

`SAMA_API_URL` is the API address. The default is staging. It must use `https`, except for
`localhost` and `127.0.0.1`. The build adds the address to `host_permissions`.

## Load it in Chrome

1. Open `chrome://extensions`.
2. Turn on "Developer mode".
3. Click "Load unpacked" and select `extension/dist`.
4. Click the sama icon in the toolbar. The side panel opens.

The API accepts every `chrome-extension://` origin unless `SAMA_EXTENSION_IDS` is set. Set it
to the id of the store item in production.

## Layout

| Path | Use |
|---|---|
| `manifest.json` | The manifest. `build.mjs` adds the API origin. |
| `src/background.ts` | The service worker: tokens, the WebSocket, the REST proxy, tab bindings. |
| `src/panel/` | The side panel: HTML, CSS (tokens from `web/app/tokens.css`), and the UI code. |
| `src/lib/` | Code without Chrome calls, with unit tests (`*.test.ts`). |
| `static/icons/` | The icons. |

## Publish

See `docs/architecture.md` §14.2.3 for the Chrome Web Store steps.
