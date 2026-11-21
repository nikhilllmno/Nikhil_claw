// The settings of a run. Each one has a default for the staging deployment, except the
// Supabase service key: it is a secret, so it comes only from the environment.

export interface Env {
  /** The web app (Vercel staging project). */
  webUrl: string;
  /** The sama API (Fly app sama-staging). */
  apiUrl: string;
  /** The service_role key of the staging Supabase project. It makes and removes test users. */
  supabaseServiceKey: string;
  /** A short id of this run. It is in each test email address, so a sweep finds the leftovers. */
  runId: string;
}

export const DEFAULT_WEB_URL = "https://sama-web-staging.vercel.app";
export const DEFAULT_API_URL = "https://sama-staging.fly.dev";

function trimSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export function readEnv(source: NodeJS.ProcessEnv = process.env): Env {
  const supabaseServiceKey = source.E2E_SUPABASE_SERVICE_ROLE_KEY ?? "";
  if (!supabaseServiceKey) {
    throw new Error(
      "Set E2E_SUPABASE_SERVICE_ROLE_KEY: the service_role key of the staging Supabase project.",
    );
  }
  return {
    webUrl: trimSlash(source.E2E_WEB_URL || DEFAULT_WEB_URL),
    apiUrl: trimSlash(source.E2E_API_URL || DEFAULT_API_URL),
    supabaseServiceKey,
    runId: source.E2E_RUN_ID || newRunId(),
  };
}

/** A run id: the UTC time to the minute and 4 random characters, for example 2611211530-a1b2. */
export function newRunId(now: Date = new Date(), random: () => number = Math.random): string {
  const stamp = now.toISOString().slice(2, 16).replace(/[-T:]/g, "");
  const suffix = Math.floor(random() * 36 ** 4)
    .toString(36)
    .padStart(4, "0");
  return `${stamp}-${suffix}`;
}
