// The sweep removes test data that a failed or stopped run left on staging: each Supabase user
// with a test email (lib/names.ts) that is older than the minimum age, and the test orgs that
// the user owns. It never touches a user without the test prefix.

import { SamaApi } from "./api.ts";
import { isTestEmail, ORG_PREFIX } from "./names.ts";
import type { AuthUser, SupabaseAdmin } from "./supabase.ts";

export interface SweepResult {
  users: string[];
  orgs: string[];
  errors: string[];
}

/** The test users that are old enough to remove. A running suite has younger users. */
export function staleTestUsers(users: AuthUser[], minAgeMs: number, now: number): AuthUser[] {
  return users.filter(
    (user) => isTestEmail(user.email) && now - Date.parse(user.created_at) >= minAgeMs,
  );
}

export async function sweep(
  admin: SupabaseAdmin,
  apiUrl: string,
  { minAgeMs, now = Date.now(), dryRun = false }: { minAgeMs: number; now?: number; dryRun?: boolean },
): Promise<SweepResult> {
  const result: SweepResult = { users: [], orgs: [], errors: [] };
  const all: AuthUser[] = [];
  for (let page = 1; ; page += 1) {
    const batch = await admin.listUsers(page);
    all.push(...batch);
    if (batch.length === 0 || page >= 50) break;
  }
  for (const user of staleTestUsers(all, minAgeMs, now)) {
    try {
      const tokens = await admin.tokensWithoutPassword(user.email!);
      const api = new SamaApi(apiUrl, async () => tokens.access_token);
      for (const org of (await api.me()).orgs) {
        if (org.role !== "owner" || !org.name.startsWith(ORG_PREFIX)) continue;
        if (!dryRun) await api.deleteOrg(org.id, org.slug);
        result.orgs.push(org.name);
      }
      if (!dryRun) await admin.deleteUser(user.id);
      result.users.push(user.email!);
    } catch (error) {
      result.errors.push(`${user.email}: ${String(error)}`);
    }
  }
  return result;
}
