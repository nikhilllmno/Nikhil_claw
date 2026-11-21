// The world of a run: the test users and the test org that the global setup makes. It is a
// file with mode 0600 in .state/, because it has the passwords of the test users. The global
// teardown removes the org and the users, and then the file.

import { chmodSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { SupabaseConfig } from "./supabase.ts";

export interface TestUser {
  email: string;
  password: string;
  authUserId: string;
}

export interface TestOrg {
  id: string;
  slug: string;
  name: string;
  workspaceId: string;
  profileId: string;
  /** The key of the user in `users` who owns the org. The teardown deletes it as this user. */
  owner: string;
}

export interface World {
  runId: string;
  webUrl: string;
  apiUrl: string;
  supabase: SupabaseConfig;
  users: Record<string, TestUser>;
  /** The main org of the run. Its owner is users.owner. */
  org: TestOrg;
  /** More orgs that tests made, so the teardown removes them too. */
  extraOrgs: TestOrg[];
  /** Users that tests made on their own (sign-up). The teardown removes them. */
  extraUsers: TestUser[];
}

const here = dirname(fileURLToPath(import.meta.url));
export const STATE_DIR = join(here, "..", ".state");
export const WORLD_FILE = join(STATE_DIR, "world.json");

export function saveWorld(world: World): void {
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  writeFileSync(WORLD_FILE, JSON.stringify(world, null, 2), { mode: 0o600 });
  chmodSync(WORLD_FILE, 0o600);
}

export function loadWorld(): World {
  if (!existsSync(WORLD_FILE)) {
    throw new Error(`No ${WORLD_FILE}. The global setup did not run or failed.`);
  }
  return JSON.parse(readFileSync(WORLD_FILE, "utf8")) as World;
}

/** Adds an org or a user that a test made, so the teardown removes it. */
export function recordExtra(extra: { org?: TestOrg; user?: TestUser }): void {
  const world = loadWorld();
  if (extra.org) world.extraOrgs.push(extra.org);
  if (extra.user) world.extraUsers.push(extra.user);
  saveWorld(world);
}

export function removeWorld(): void {
  rmSync(WORLD_FILE, { force: true });
}
