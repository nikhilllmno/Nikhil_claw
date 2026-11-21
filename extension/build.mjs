// Builds the extension into dist/. Load dist/ with "Load unpacked" in chrome://extensions.
//
// SAMA_API_URL sets the default API address (the staging API if it is not set). The built
// manifest has host access to that address, so the extension calls it without CORS.

import { build } from "esbuild";
import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";

const STAGING = "https://sama-staging.fly.dev";
const apiUrl = (process.env.SAMA_API_URL ?? STAGING).trim().replace(/\/+$/, "");
const api = new URL(apiUrl);
if (api.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(api.hostname)) {
  throw new Error("SAMA_API_URL must use https, except for localhost.");
}

await rm("dist", { recursive: true, force: true });
await mkdir("dist", { recursive: true });

await build({
  entryPoints: { background: "src/background.ts", panel: "src/panel/panel.ts" },
  bundle: true,
  format: "esm",
  target: "chrome116",
  outdir: "dist",
  minify: false,
  sourcemap: false,
  legalComments: "none",
  define: { __SAMA_API_URL__: JSON.stringify(apiUrl) },
  logLevel: "warning",
});

await cp("static", "dist", { recursive: true });
await cp("src/panel/panel.html", "dist/panel.html");
await cp("src/panel/panel.css", "dist/panel.css");
await cp("src/panel/tokens.css", "dist/tokens.css");

const manifest = JSON.parse(await readFile("manifest.json", "utf8"));
const origin = `${api.origin}/*`;
if (!manifest.host_permissions.includes(origin)) manifest.host_permissions.push(origin);
await writeFile("dist/manifest.json", `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Built dist/ for ${apiUrl}`);
