// The Supabase Auth admin calls that make and remove the test users. Only the setup, the
// teardown, and the sweep use the service key. The tests use user tokens only.

import { requestJson } from "./http.ts";

export interface SupabaseConfig {
  url: string;
  anonKey: string;
}

export interface AuthUser {
  id: string;
  email: string | null;
  created_at: string;
}

export interface Tokens {
  access_token: string;
  refresh_token: string | null;
  /** Epoch milliseconds, as the web app keeps it (web/lib/auth.ts). */
  expires_at: number;
}

export class SupabaseAdmin {
  readonly config: SupabaseConfig;
  private readonly serviceKey: string;

  constructor(config: SupabaseConfig, serviceKey: string) {
    this.config = config;
    this.serviceKey = serviceKey;
  }

  private admin(): Record<string, string> {
    return { apikey: this.serviceKey, authorization: `Bearer ${this.serviceKey}` };
  }

  /** A user with a confirmed email address and a password. No email is sent. */
  async createUser(email: string, password: string): Promise<AuthUser> {
    const { data } = await requestJson<AuthUser>(`${this.config.url}/auth/v1/admin/users`, {
      method: "POST",
      headers: this.admin(),
      body: { email, password, email_confirm: true },
    });
    return data;
  }

  /**
   * Signs a user up as `/auth/v1/signup` does, but without the confirmation email: the staging
   * project has the built-in mailer, which sends only to the team (docs/architecture.md,
   * Section 17.2). The user is not confirmed yet.
   */
  async signUpWithoutEmail(email: string, password: string): Promise<AuthUser> {
    const { data } = await requestJson<AuthUser & { user?: AuthUser }>(
      `${this.config.url}/auth/v1/admin/generate_link`,
      { method: "POST", headers: this.admin(), body: { type: "signup", email, password } },
    );
    return data.user ?? data;
  }

  /** Confirms the email address, as the link of the confirmation email does. */
  async confirmEmail(userId: string): Promise<void> {
    await requestJson(`${this.config.url}/auth/v1/admin/users/${userId}`, {
      method: "PUT",
      headers: this.admin(),
      body: { email_confirm: true },
    });
  }

  async deleteUser(userId: string): Promise<void> {
    await requestJson(`${this.config.url}/auth/v1/admin/users/${userId}`, {
      method: "DELETE",
      headers: this.admin(),
      allow: [404],
    });
  }

  /**
   * The tokens of a user without the password, for the sweep: an admin magic link, verified at
   * once. No email is sent.
   */
  async tokensWithoutPassword(email: string, now: () => number = Date.now): Promise<Tokens> {
    const { data: link } = await requestJson<{
      hashed_token?: string;
      properties?: { hashed_token?: string };
    }>(`${this.config.url}/auth/v1/admin/generate_link`, {
      method: "POST",
      headers: this.admin(),
      body: { type: "magiclink", email },
    });
    const hash = link.properties?.hashed_token ?? link.hashed_token;
    if (!hash) throw new Error(`No magic link for ${email}.`);
    const { data } = await requestJson<{ access_token: string; expires_in?: number }>(
      `${this.config.url}/auth/v1/verify`,
      { method: "POST", headers: { apikey: this.config.anonKey }, body: { type: "magiclink", token_hash: hash } },
    );
    return {
      access_token: data.access_token,
      refresh_token: null,
      expires_at: now() + (data.expires_in ?? 3600) * 1000,
    };
  }

  async listUsers(page: number, perPage = 200): Promise<AuthUser[]> {
    const { data } = await requestJson<{ users: AuthUser[] }>(
      `${this.config.url}/auth/v1/admin/users?page=${page}&per_page=${perPage}`,
      { headers: this.admin() },
    );
    return data.users ?? [];
  }
}

/** The tokens of a user, from the password grant, as the web app signs in. */
export async function signIn(
  config: SupabaseConfig,
  email: string,
  password: string,
  now: () => number = Date.now,
): Promise<Tokens> {
  const { data } = await requestJson<{
    access_token: string;
    refresh_token?: string;
    expires_in?: number;
  }>(`${config.url}/auth/v1/token?grant_type=password`, {
    method: "POST",
    headers: { apikey: config.anonKey },
    body: { email, password },
  });
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token ?? null,
    expires_at: now() + (data.expires_in ?? 3600) * 1000,
  };
}

/** The Supabase URL and anon key come from the API, as the web app reads them. */
export async function supabaseConfig(apiUrl: string): Promise<SupabaseConfig> {
  const { data } = await requestJson<{ supabase_url?: string; supabase_anon_key?: string }>(
    `${apiUrl}/config`,
  );
  if (!data.supabase_url || !data.supabase_anon_key) {
    throw new Error("GET /config has no Supabase settings: sign-in is off on this API.");
  }
  return { url: data.supabase_url.replace(/\/+$/, ""), anonKey: data.supabase_anon_key };
}
