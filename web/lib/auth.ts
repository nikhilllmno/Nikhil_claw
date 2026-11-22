// Sign-in with the Supabase Auth REST API (the same calls as the minimal page of the API).
// Tokens live in sessionStorage. Nothing reads a token from the URL.
//
// A new tab of the same browser (for example a link from the chat) asks the open tabs for the
// tokens over a BroadcastChannel (M8.1), so it opens signed in. The tokens stay in memory and
// in sessionStorage: they are not written to localStorage, and they end with the last tab.
// A sign-in, a refresh, and a sign-out in one tab go to the other tabs, so no tab keeps a
// refresh token that Supabase rotated.

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

/** The part of `BroadcastChannel` that the sign-in uses. */
export interface TabChannel {
  postMessage(message: unknown): void;
  onmessage: ((event: { data: unknown }) => void) | null;
}

export const TAB_CHANNEL = "sama.auth";
/** How long a new tab waits for an open tab to give the tokens. */
export const ADOPT_WAIT_MS = 400;

type TabMessage =
  | { type: "ask" }
  | { type: "tokens" | "signin" | "refresh"; tokens: Tokens }
  | { type: "signout" };

function tokensOf(value: unknown): Tokens | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.access_token !== "string" || raw.access_token === "") return null;
  if (typeof raw.expires_at !== "number") return null;
  return {
    access_token: raw.access_token,
    refresh_token: typeof raw.refresh_token === "string" ? raw.refresh_token : null,
    expires_at: raw.expires_at,
  };
}

function messageOf(value: unknown): TabMessage | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (raw.type === "ask" || raw.type === "signout") return { type: raw.type };
  if (raw.type === "tokens" || raw.type === "signin" || raw.type === "refresh") {
    const tokens = tokensOf(raw.tokens);
    return tokens === null ? null : { type: raw.type, tokens };
  }
  return null;
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
  /** The channel to the other tabs of this browser (M8.1). Without it, each tab signs in alone. */
  tabs?: TabChannel | null;
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

  private waiting: ((tokens: Tokens) => void) | null = null;

  constructor(private readonly deps: AuthDeps) {
    this.fetcher = deps.fetcher ?? ((input, init) => fetch(input, init));
    this.now = deps.now ?? (() => Date.now());
    if (deps.tabs) deps.tabs.onmessage = (event) => this.fromTab(event.data);
  }

  private tell(message: TabMessage) {
    try {
      this.deps.tabs?.postMessage(message);
    } catch {
      // A closed channel: the other tabs keep their own state.
    }
  }

  /** A message of another tab of this browser. */
  private fromTab(data: unknown) {
    const message = messageOf(data);
    if (message === null) return;
    switch (message.type) {
      case "ask":
        if (this.tokens !== null && this.tokens.expires_at > this.now()) {
          this.tell({ type: "tokens", tokens: this.tokens });
        }
        return;
      case "tokens":
        if (this.waiting !== null) this.waiting(message.tokens);
        return;
      case "signin":
        // Another tab signed in, maybe as another account: a new generation.
        this.epoch += 1;
        this.inflight = null;
        this.keep(message.tokens);
        return;
      case "refresh":
        if (this.tokens !== null) this.keep(message.tokens);
        return;
      case "signout":
        if (this.tokens !== null) this.signOut(undefined, { fromTab: true });
        return;
    }
  }

  private keep(tokens: Tokens) {
    this.tokens = tokens;
    this.deps.session.setItem(AUTH_KEY, JSON.stringify(tokens));
    this.deps.onChange?.(tokens);
  }

  /**
   * A tab without tokens asks the open tabs of this browser for them (M8.1). Resolves with the
   * tokens, or `null` after `waitMs` when no tab has them.
   */
  adoptFromTabs(waitMs: number = ADOPT_WAIT_MS): Promise<Tokens | null> {
    if (this.tokens !== null || !this.deps.tabs) return Promise.resolve(this.tokens);
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.waiting = null;
        resolve(null);
      }, waitMs);
      this.waiting = (tokens) => {
        clearTimeout(timer);
        this.waiting = null;
        if (this.tokens === null && tokens.expires_at > this.now()) {
          this.epoch += 1;
          this.inflight = null;
          this.keep(tokens);
        }
        resolve(this.tokens);
      };
      this.tell({ type: "ask" });
    });
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
    this.store(data, "signin");
  }

  private store(data: TokenData, kind: "signin" | "refresh" = "refresh") {
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
    this.tell({ type: kind, tokens: this.tokens });
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

  /**
   * Makes a Supabase Auth account. When the project needs a confirmed email, Supabase emails a
   * link that comes back to `redirectTo` with a `code` (PKCE), and this returns `"confirm"`.
   * Otherwise the user is signed in and this returns `"signed-in"`.
   */
  async signUp(email: string, password: string, redirectTo: string): Promise<"confirm" | "signed-in"> {
    const challenge = await newPkce(this.deps.local, this.now());
    const data = await this.auth(`signup?redirect_to=${encodeURIComponent(redirectTo)}`, {
      email,
      password,
      code_challenge: challenge,
      code_challenge_method: "s256",
    });
    if (typeof data.access_token === "string" && data.access_token !== "") {
      this.signedIn(data);
      return "signed-in";
    }
    return "confirm";
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
      // Only this tab: a token that cannot refresh here can still work in another tab.
      this.signOut(undefined, { fromTab: true });
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
        // Only this tab: a network error here must not sign out the other tabs.
        if (epoch === this.epoch) this.signOut(undefined, { fromTab: true });
        return null;
      } finally {
        if (epoch === this.epoch) this.inflight = null;
      }
    };
    this.inflight = run();
    return this.inflight;
  }

  /** Signs out. With `generation`: only if it is the current one. The other tabs sign out too. */
  signOut(generation?: number, options: { fromTab?: boolean } = {}): void {
    if (generation !== undefined && generation !== this.epoch) return;
    this.epoch += 1;
    this.inflight = null;
    this.tokens = null;
    this.deps.session.removeItem(AUTH_KEY);
    this.deps.onChange?.(null);
    if (!options.fromTab) this.tell({ type: "signout" });
  }
}
