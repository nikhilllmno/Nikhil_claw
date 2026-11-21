// Removal of test data: the orgs first (as their owner, with the API, so sama removes all rows
// of the org), then the Supabase users. Each step goes on when one fails, and the errors are
// returned, so one bad row does not keep the rest.

import { SamaApi } from "./api.ts";
import { signIn, type SupabaseAdmin, type SupabaseConfig } from "./supabase.ts";
import type { TestOrg, TestUser } from "./world.ts";

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
  admin: SupabaseAdmin,
  apiUrl: string,
  orgs: { org: TestOrg; owner: TestUser | undefined }[],
  users: TestUser[],
): Promise<string[]> {
  const errors: string[] = [];
  for (const { org, owner } of orgs) {
    if (!owner) {
      errors.push(`org ${org.id}: no owner in the world file`);
      continue;
    }
    try {
      await deleteOrgAs(apiUrl, admin.config, owner, org);
    } catch (error) {
      errors.push(`org ${org.id}: ${String(error)}`);
    }
  }
  for (const user of users) {
    try {
      await admin.deleteUser(user.authUserId);
    } catch (error) {
      errors.push(`user ${user.email}: ${String(error)}`);
    }
  }
  return errors;
}
