// The Playwright fixtures of the suite: the world of the run, an API client for each test user,
// and a page where a test user is already signed in.

import { test as base, expect, type Page } from "@playwright/test";
import { SamaApi } from "./lib/api.ts";
import { readEnv } from "./lib/env.ts";
import { TokenCache } from "./lib/session.ts";
import { SupabaseAdmin, type Tokens } from "./lib/supabase.ts";
import { loadWorld, type World } from "./lib/world.ts";

/** The keys that the web app reads (web/lib/auth.ts, web/components/DataProvider.tsx). */
export const AUTH_KEY = "sama.auth";
export const WORKSPACE_KEY = "sama.workspace";

/** Puts the tokens in sessionStorage before any script of the app runs, as a sign-in does. */
export async function signInPage(page: Page, tokens: Tokens, workspaceId?: string): Promise<void> {
  await page.addInitScript(
    ({ authKey, auth, workspaceKey, workspace }) => {
      // Only once for each tab, so a sign-out in the test stays signed out.
      if (window.sessionStorage.getItem("sama.e2e.injected") === "1") return;
      window.sessionStorage.setItem("sama.e2e.injected", "1");
      window.sessionStorage.setItem(authKey, auth);
      if (workspace) window.localStorage.setItem(workspaceKey, workspace);
    },
    {
      authKey: AUTH_KEY,
      auth: JSON.stringify(tokens),
      workspaceKey: WORKSPACE_KEY,
      workspace: workspaceId ?? null,
    },
  );
}

interface WorkerFixtures {
  world: World;
  tokenCache: TokenCache;
  /** The Supabase admin client. Only the tests of sign-up use it. */
  supabaseAdmin: SupabaseAdmin;
}

interface TestFixtures {
  ownerApi: SamaApi;
  memberApi: SamaApi;
  /** A page where the owner of the test org is signed in, on the workspace of the org. */
  ownerPage: Page;
}

export const test = base.extend<TestFixtures, WorkerFixtures>({
  world: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await use(loadWorld());
    },
    { scope: "worker" },
  ],
  tokenCache: [
    async ({ world }, use) => {
      await use(new TokenCache(world));
    },
    { scope: "worker" },
  ],
  supabaseAdmin: [
    async ({ world }, use) => {
      await use(new SupabaseAdmin(world.supabase, readEnv().supabaseServiceKey));
    },
    { scope: "worker" },
  ],
  ownerApi: async ({ world, tokenCache }, use) => {
    await use(
      new SamaApi(world.apiUrl, async () => (await tokenCache.forUser("owner")).access_token),
    );
  },
  memberApi: async ({ world, tokenCache }, use) => {
    await use(
      new SamaApi(world.apiUrl, async () => (await tokenCache.forUser("member")).access_token),
    );
  },
  ownerPage: async ({ page, world, tokenCache }, use) => {
    await signInPage(page, await tokenCache.forUser("owner"), world.org.workspaceId);
    await use(page);
  },
});

export { expect };
