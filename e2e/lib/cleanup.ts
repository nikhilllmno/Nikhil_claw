// Removal of test data: the orgs first (as their owner, with the API, so sama removes all rows
// of the org), then the Supabase users. A user who still owns an org that did not go is kept,
// so the sweep can sign in as that user later and try again (lib/sweep.ts).

import { SamaApi } from "./api.ts";
import { HttpError } from "./http.ts";
import { signIn, type SupabaseConfig } from "./supabase.ts";
import type { TestOrg, TestUser } from "./world.ts";

export interface CleanupDeps {
  deleteOrg: (owner: TestUser, org: TestOrg) => Promise<unknown>;
  deleteUser: (user: TestUser) => Promise<unknown>;
  /** Waits between tries. Tests pass a function that does not wait. */
  sleep?: (ms: number) => Promise<void>;
}

/** Tries of an org delete. The API answers 409 while runs of the org stop (Section 17.4). */
export const ORG_DELETE_TRIES = 5;
export const ORG_DELETE_WAIT_MS = 10_000;

/** True for a failure that can pass: a conflict, a server error, or no answer. */
export function transient(error: unknown): boolean {
  if (error instanceof HttpError) return error.status === 409 || error.status >= 500;
  return true;
}

export async function deleteOrgAs(
  apiUrl: string,
  supabase: SupabaseConfig,
  owner: TestUser,
  org: Pick<TestOrg, "id" | "slug">,
): Promise<number> {
  const tokens = await signIn(supabase, owner.email, owner.password);
  const api = new SamaApi(apiUrl, async () => tokens.access_token);
  return api.deleteOrg(org.id, org.slug);
}

export async function cleanUp(
  orgs: { org: TestOrg; owner: TestUser | undefined }[],
  users: TestUser[],
  deps: CleanupDeps,
): Promise<string[]> {
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const errors: string[] = [];
  const keep = new Set<string>();
  for (const { org, owner } of orgs) {
    if (!owner) {
      errors.push(`org ${org.id}: no owner in the world file`);
      continue;
    }
    for (let attempt = 1; ; attempt += 1) {
      try {
        await deps.deleteOrg(owner, org);
        break;
      } catch (error) {
        if (attempt < ORG_DELETE_TRIES && transient(error)) {
          await sleep(ORG_DELETE_WAIT_MS * attempt);
          continue;
        }
        errors.push(`org ${org.id}: ${String(error)}`);
        keep.add(owner.email);
        break;
      }
    }
  }
  for (const user of users) {
    if (keep.has(user.email)) {
      errors.push(`user ${user.email}: kept, because an org of the user is still there`);
      continue;
    }
    try {
      await deps.deleteUser(user);
    } catch (error) {
      errors.push(`user ${user.email}: ${String(error)}`);
    }
  }
  return errors;
}
