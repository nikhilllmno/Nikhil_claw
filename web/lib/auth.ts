// Sign-in with the Supabase Auth REST API (the same calls as the minimal page of the API).
// Tokens live in sessionStorage. Nothing reads a token from the URL.

import type { FetchLike, TokenSource } from "./api";
import { type KeyValueStore, newPkce, takeVerifier } from "./pkce";

export const AUTH_KEY = "sama.auth";
/** Refresh this long before the token expires. */
export const REFRESH_MARGIN_MS = 60_000;

export interface Tokens {
  access_token: string;
  refresh_token: string | null;
  /** Epoch milliseconds. */
  expires_at: number;
}

export interface SupabaseConfig {
  url: string;
  anonKey: string;
}

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}

export interface AuthDeps {
  config: SupabaseConfig | null;
  /** sessionStorage: the tokens. */
  session: KeyValueStore;
  /** localStorage: the PKCE verifier, because the sign-in link can open in a new tab. */
  local: KeyValueStore;
  fetcher?: FetchLike;
  now?: () => number;
  /** Called when the signed-in state changes. */
  onChange?: (tokens: Tokens | null) => void;
}

interface TokenData {
  access_token?: unknown;
  refresh_token?: unknown;
  expires_in?: unknown;
}

export class AuthSession implements TokenSource {
  private tokens: Tokens | null = null;
  private inflight: Promise<string | null> | null = null;
  /** Grows at each sign-out and each new sign-in, so that a late answer for the old user is dropped. */
  private epoch = 0;
  private readonly fetcher: FetchLike;
  private readonly now: () => number;

  constructor(private readonly deps: AuthDeps) {
    this.fetcher = deps.fetcher ?? ((input, init) => fetch(input, init));
    this.now = deps.now ?? (() => Date.now());
  }

  /** Reads the tokens that sessionStorage keeps. Returns them, or `null`. */
  load(): Tokens | null {
    const raw = this.deps.session.getItem(AUTH_KEY);
    this.tokens = null;
    if (raw !== null) {
      try {
        const saved = JSON.parse(raw) as Partial<Tokens>;
        if (typeof saved.access_token === "string" && typeof saved.expires_at === "number") {
          this.tokens = {
            access_token: saved.access_token,
            refresh_token: typeof saved.refresh_token === "string" ? saved.refresh_token : null,
            expires_at: saved.expires_at,
          };
        }
      } catch {
        this.tokens = null;
      }
    }
    return this.tokens;
  }

  get current(): Tokens | null {
    return this.tokens;
  }

  /** Changes when the account changes (sign-out or a new sign-in), not at a token refresh. */
  get generation(): number {
    return this.epoch;
  }

  /** A sign-in starts a new generation: the answers that wait for the old one are dropped. */
  private signedIn(data: TokenData) {
    this.epoch += 1;
    this.inflight = null;
    this.store(data);
  }

  private store(data: TokenData) {
    if (typeof data.access_token !== "string" || data.access_token === "") {
      throw new AuthError("Sign-in failed.");
    }
    const expiresIn = Number(data.expires_in ?? 3600);
    this.tokens = {
      access_token: data.access_token,
      refresh_token: typeof data.refresh_token === "string" ? data.refresh_token : null,
      expires_at: this.now() + (Number.isFinite(expiresIn) ? expiresIn : 3600) * 1000,
    };
    this.deps.session.setItem(AUTH_KEY, JSON.stringify(this.tokens));
    this.deps.onChange?.(this.tokens);
  }

  private async auth(path: string, body: unknown): Promise<Record<string, unknown>> {
    const config = this.deps.config;
    if (!config) throw new AuthError("Sign-in is not configured. Use an access token.");
    const response = await this.fetcher(`${config.url}/auth/v1/${path}`, {
      method: "POST",
      headers: { apikey: config.anonKey, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) {
      const text = data.error_description ?? data.msg ?? data.message;
      throw new AuthError(typeof text === "string" && text ? text : "Sign-in failed.");
    }
    return data;
  }

  async signInWithPassword(email: string, password: string): Promise<void> {
    this.signedIn(await this.auth("token?grant_type=password", { email, password }));
  }

  /** Asks Supabase to email a sign-in link that comes back to `redirectTo` with a `code`. */
  async sendSignInLink(email: string, redirectTo: string): Promise<void> {
    const challenge = await newPkce(this.deps.local, this.now());
    await this.auth(`otp?redirect_to=${encodeURIComponent(redirectTo)}`, {
      email,
      create_user: false,
      code_challenge: challenge,
      code_challenge_method: "s256",
    });
  }

  /** Uses the `code` of a sign-in link. Throws if this browser did not ask for the link. */
  async exchangeCode(code: string): Promise<void> {
    const verifier = takeVerifier(this.deps.local, this.now());
    if (verifier === null) {
      throw new AuthError(
        "This sign-in link is not from this browser, or it is too old. Ask for a new link.",
      );
    }
    this.signedIn(await this.auth("token?grant_type=pkce", { auth_code: code, code_verifier: verifier }));
  }

  /** Uses an access token that the user pastes (development). It cannot refresh. */
  useAccessToken(token: string): void {
    this.signedIn({ access_token: token.trim(), expires_in: 3600 });
  }

  async getToken(): Promise<string> {
    const tokens = this.tokens;
    if (tokens === null) throw new AuthError("Sign in first.");
    if (tokens.refresh_token !== null && this.now() > tokens.expires_at - REFRESH_MARGIN_MS) {
      const token = await this.refresh(this.epoch);
      if (token === null) throw new AuthError("Your sign-in ended. Sign in again.");
      return token;
    }
    return tokens.access_token;
  }

  /** One refresh at a time: calls that overlap share the same answer. */
  refresh(generation: number): Promise<string | null> {
    if (generation !== this.epoch) return Promise.resolve(null);
    if (this.inflight !== null) return this.inflight;
    const refreshToken = this.tokens?.refresh_token ?? null;
    if (refreshToken === null) {
      this.signOut();
      return Promise.resolve(null);
    }
    const epoch = this.epoch;
    const run = async (): Promise<string | null> => {
      try {
        const data = await this.auth("token?grant_type=refresh_token", {
          refresh_token: refreshToken,
        });
        if (epoch !== this.epoch) return null;
        this.store(data);
        return this.tokens?.access_token ?? null;
      } catch {
        if (epoch === this.epoch) this.signOut();
        return null;
      } finally {
        if (epoch === this.epoch) this.inflight = null;
      }
    };
    this.inflight = run();
    return this.inflight;
  }

  /** Signs out. With `generation`: only if it is the current one. */
  signOut(generation?: number): void {
    if (generation !== undefined && generation !== this.epoch) return;
    this.epoch += 1;
    this.inflight = null;
    this.tokens = null;
    this.deps.session.removeItem(AUTH_KEY);
    this.deps.onChange?.(null);
  }
}
