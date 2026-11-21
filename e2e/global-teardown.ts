// Removes the world of the run: the test orgs and the test users. Set E2E_KEEP_DATA=1 to keep
// them for a look after a failure. When a step fails, the world file stays, and the users that
// still own an org stay too, so the nightly sweep can remove them later.

import { cleanUp, deleteOrgAs } from "./lib/cleanup.ts";
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
  if (world.supabase.url !== env.supabaseUrl) {
    throw new Error(`The world file is for ${world.supabase.url}, not ${env.supabaseUrl}.`);
  }
  const admin = new SupabaseAdmin(world.supabase, env.supabaseServiceKey);
  const orgs = [world.org, ...world.extraOrgs]
    .filter((org) => org.id)
    .map((org) => ({
      org,
      owner: world.users[org.owner] ?? world.extraUsers.find((u) => u.email === org.owner),
    }));
  const users = [...Object.values(world.users), ...world.extraUsers];
  const errors = await cleanUp(orgs, users, {
    deleteOrg: (owner, org) => deleteOrgAs(world.apiUrl, world.supabase, owner, org),
    deleteUser: (user) => admin.deleteUser(user.authUserId),
  });
  if (errors.length > 0) {
    throw new Error(`The cleanup left test data on staging:\n${errors.join("\n")}`);
  }
  removeWorld();
}
