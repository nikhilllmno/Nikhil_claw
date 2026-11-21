// Makes the world of a run on staging: two confirmed test users (an owner and a member) and
// one test org on the free plan that the owner made with self-serve sign-up. No email is sent.

import { randomBytes } from "node:crypto";
import { SamaApi } from "./lib/api.ts";
import { readEnv } from "./lib/env.ts";
import { newPassword, orgName, testEmail } from "./lib/names.ts";
import { signIn, SupabaseAdmin, supabaseConfig } from "./lib/supabase.ts";
import { existsSync } from "node:fs";
import { saveWorld, type TestUser, type World, WORLD_FILE } from "./lib/world.ts";

const USERS = ["owner", "member"] as const;

export default async function globalSetup(): Promise<void> {
  // For work on a test: keep the world of the last run (with E2E_KEEP_DATA=1) and use it again.
  if (process.env.E2E_REUSE_WORLD === "1" && existsSync(WORLD_FILE)) return;
  const env = readEnv();
  const supabase = await supabaseConfig(env.apiUrl, env.supabaseUrl);
  const admin = new SupabaseAdmin(supabase, env.supabaseServiceKey);

  const users: Record<string, TestUser> = {};
  for (const key of USERS) {
    const email = testEmail(env.runId, key);
    const password = newPassword((n) => randomBytes(n));
    const created = await admin.createUser(email, password);
    users[key] = { email, password, authUserId: created.id };
  }
  const owner = users.owner!;
  // Save the users at once, so the teardown removes them also when the org step fails.
  const world: World = {
    runId: env.runId,
    webUrl: env.webUrl,
    apiUrl: env.apiUrl,
    supabase,
    users,
    org: { id: "", slug: "", name: "", workspaceId: "", profileId: "", owner: "owner" },
    extraOrgs: [],
    extraUsers: [],
  };
  saveWorld(world);

  const tokens = await signIn(supabase, owner.email, owner.password);
  const api = new SamaApi(env.apiUrl, async () => tokens.access_token);
  const name = orgName(env.runId);
  const org = await api.createOrg(name);
  world.org = {
    id: org.id,
    slug: org.slug,
    name,
    workspaceId: org.workspace_id,
    profileId: org.agent_profile_id,
    owner: "owner",
  };
  saveWorld(world);
}
