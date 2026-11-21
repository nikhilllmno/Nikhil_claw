// Sign-in with the Supabase Auth REST API, like web/lib/auth.ts. The service worker owns the
// tokens. They live in chrome.storage.session: in memory, only for extension pages, and gone
// when the browser closes.

export const AUTH_KEY = "sama.auth";
/** Refresh this long before the token expires. */
export const REFRESH_MARGIN_MS = 60_000;

export interface Tokens {
  access_token: string;
  refresh_token: string | null;
  /** Epoch milliseconds. */
  expires_at: number;
  /** The email of a password sign-in, for the panel. */
  email: string | null;
}

export interface SupabaseConfig {
  url: string;
  anonKey: string;
}

/** The part of chrome.storage that the code uses (tests give a map). */
export interface AsyncStore {
  get(key: string): Promise<unknown>;
  set(key: string, value: unknown): Promise<void>;
  remove(key: string): Promise<void>;
  keys(): Promise<string[]>;
}

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}

/** The sign-in service did not answer. */
export class AuthNetworkError extends AuthError {}

export interface AuthDeps {
  store: AsyncStore;
  /** The Supabase settings of the API (`GET /config`), or `null` if it has none. */
  config: () => Promise<SupabaseConfig | null>;
  fetcher?: FetchLike;
  now?: () => number;
  onChange?: (tokens: Tokens | null) => void;
}

interface TokenData {
  access_token?: unknown;
  refresh_token?: unknown;
  expires_in?: unknown;
}

export class Auth {
  private tokens: Tokens | null = null;
  private inflight: Promise<string | null> | null = null;
  private epoch = 0;
  private readonly fetcher: FetchLike;
  private readonly now: () => number;

  constructor(private readonly deps: AuthDeps) {
    this.fetcher = deps.fetcher ?? ((input, init) => fetch(input, init));
    this.now = deps.now ?? (() => Date.now());
  }

  get current(): Tokens | null {
    return this.tokens;
  }

  async load(): Promise<Tokens | null> {
    const saved = (await this.deps.store.get(AUTH_KEY)) as Partial<Tokens> | undefined;
    this.tokens =
      saved && typeof saved.access_token === "string" && typeof saved.expires_at === "number"
        ? {
            access_token: saved.access_token,
            refresh_token: typeof saved.refresh_token === "string" ? saved.refresh_token : null,
            expires_at: saved.expires_at,
            email: typeof saved.email === "string" ? saved.email : null,
          }
        : null;
    return this.tokens;
  }

  private async save(data: TokenData, email: string | null): Promise<void> {
    if (typeof data.access_token !== "string" || data.access_token === "") {
      throw new AuthError("Sign-in failed.");
    }
    const expiresIn = Number(data.expires_in ?? 3600);
    this.tokens = {
      access_token: data.access_token,
      refresh_token: typeof data.refresh_token === "string" ? data.refresh_token : null,
      expires_at: this.now() + (Number.isFinite(expiresIn) ? expiresIn : 3600) * 1000,
      email,
    };
    await this.deps.store.set(AUTH_KEY, this.tokens);
    this.deps.onChange?.(this.tokens);
  }

  private async call(path: string, body: unknown): Promise<TokenData> {
    const config = await this.deps.config();
    if (!config) throw new AuthError("Password sign-in is not set up on this API. Use an access token.");
    const response = await this.fetcher(`${config.url}/auth/v1/${path}`, {
      method: "POST",
      headers: { apikey: config.anonKey, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).catch(() => {
      throw new AuthNetworkError("sama could not reach the sign-in service. Check the connection and try again.");
    });
    const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
    if (!response.ok) {
      const text = data.error_description ?? data.msg ?? data.message;
      throw new AuthError(typeof text === "string" && text ? text : "Sign-in failed.");
    }
    return data;
  }

  async signInWithPassword(email: string, password: string): Promise<void> {
    // Take the epoch first: a later sign-in or sign-out wins over this one when it ends first.
    const epoch = ++this.epoch;
    this.inflight = null;
    const data = await this.call("token?grant_type=password", { email, password });
    if (epoch !== this.epoch) throw new AuthError("A newer sign-in or sign-out replaced this one.");
    await this.save(data, email.trim());
  }

  /** Uses an access token that the user pastes (development). It cannot refresh. */
  async useAccessToken(token: string): Promise<void> {
    const value = token.trim();
    if (!value) throw new AuthError("Paste an access token.");
    this.epoch += 1;
    this.inflight = null;
    await this.save({ access_token: value, expires_in: jwtExpiresIn(value, this.now()) }, null);
  }

  /** The access token to use now. A token that is about to expire is refreshed first. */
  async getToken(): Promise<string | null> {
    const tokens = this.tokens;
    if (tokens === null) return null;
    if (this.now() > tokens.expires_at - REFRESH_MARGIN_MS) {
      if (tokens.refresh_token === null) {
        return this.now() < tokens.expires_at ? tokens.access_token : null;
      }
      return this.refresh();
    }
    return tokens.access_token;
  }

  /** Gets a new access token. One refresh at a time. Signs out if Supabase refuses the refresh. */
  refresh(): Promise<string | null> {
    if (this.inflight !== null) return this.inflight;
    const refreshToken = this.tokens?.refresh_token ?? null;
    if (refreshToken === null) return Promise.resolve(null);
    const epoch = this.epoch;
    const email = this.tokens?.email ?? null;
    const run = async (): Promise<string | null> => {
      try {
        const data = await this.call("token?grant_type=refresh_token", { refresh_token: refreshToken });
        if (epoch !== this.epoch) return null;
        await this.save(data, email);
        return this.tokens?.access_token ?? null;
      } catch (error) {
        if (epoch !== this.epoch) return null;
        // A network failure keeps the session: the access token can still be valid for a
        // short time, and the next call tries the refresh again.
        if (error instanceof AuthNetworkError) {
          const tokens = this.tokens;
          return tokens !== null && this.now() < tokens.expires_at ? tokens.access_token : null;
        }
        await this.signOut();
        return null;
      } finally {
        if (epoch === this.epoch) this.inflight = null;
      }
    };
    this.inflight = run();
    return this.inflight;
  }

  async signOut(): Promise<void> {
    this.epoch += 1;
    this.inflight = null;
    this.tokens = null;
    await this.deps.store.remove(AUTH_KEY);
    this.deps.onChange?.(null);
  }
}

/** The seconds until the `exp` claim of a JWT, or one hour if the token has no `exp`. */
export function jwtExpiresIn(token: string, now: number): number {
  const part = token.split(".")[1];
  if (!part) return 3600;
  try {
    const json = atob(part.replace(/-/g, "+").replace(/_/g, "/"));
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    if (typeof exp === "number" && Number.isFinite(exp)) return Math.max(0, exp - now / 1000);
  } catch {
    // Not a JWT: the server decides.
  }
  return 3600;
}
