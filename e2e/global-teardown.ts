// Removes the world of the run: the test orgs and the test users. Set E2E_KEEP_DATA=1 to keep
// them for a look after a failure; the nightly sweep removes them a day later.

import { cleanUp } from "./lib/cleanup.ts";
import { readEnv } from "./lib/env.ts";
import { SupabaseAdmin } from "./lib/supabase.ts";
import { loadWorld, removeWorld } from "./lib/world.ts";

export default async function globalTeardown(): Promise<void> {
  if (process.env.E2E_KEEP_DATA === "1") {
    console.warn("E2E_KEEP_DATA=1: the test org and users stay on staging.");
    return;
  }
  const env = readEnv();
  const world = loadWorld();
  const admin = new SupabaseAdmin(world.supabase, env.supabaseServiceKey);
  const orgs = [world.org, ...world.extraOrgs]
    .filter((org) => org.id)
    .map((org) => ({
      org,
      owner: world.users[org.owner] ?? world.extraUsers.find((u) => u.email === org.owner),
    }));
  const users = [...Object.values(world.users), ...world.extraUsers];
  const errors = await cleanUp(admin, world.apiUrl, orgs, users);
  removeWorld();
  if (errors.length > 0) {
    throw new Error(`The cleanup left test data on staging:\n${errors.join("\n")}`);
  }
}
