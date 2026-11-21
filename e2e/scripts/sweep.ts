// Removes the test data that old runs left on staging. The nightly workflow runs it before the
// suite. Usage: E2E_SUPABASE_SERVICE_ROLE_KEY=... npm run sweep [-- --dry-run]
// E2E_SWEEP_MIN_AGE_HOURS (default 3) keeps the users of a run that is still going.

import { readEnv } from "../lib/env.ts";
import { SupabaseAdmin, supabaseConfig } from "../lib/supabase.ts";
import { sweep } from "../lib/sweep.ts";

const env = readEnv();
const hours = Number(process.env.E2E_SWEEP_MIN_AGE_HOURS || 3);
const dryRun = process.argv.includes("--dry-run");
const admin = new SupabaseAdmin(await supabaseConfig(env.apiUrl, env.supabaseUrl), env.supabaseServiceKey);
const result = await sweep(admin, env.apiUrl, { minAgeMs: hours * 3_600_000, dryRun });
console.warn(
  `${dryRun ? "Would remove" : "Removed"} ${result.users.length} test user(s) and ` +
    `${result.orgs.length} test org(s) older than ${hours} hour(s).`,
);
for (const error of result.errors) console.error(`Not removed: ${error}`);
process.exitCode = result.errors.length > 0 ? 1 : 0;
