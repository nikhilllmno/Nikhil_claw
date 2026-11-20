// PKCE for the sign-in link (the same flow as the minimal page of the API).
// The page that asks for the link keeps the verifier. Only this browser can use the code in
// the link, so a link from another person cannot sign this browser in.

export const PKCE_KEY = "sama.pkce";
export const PKCE_MAX_AGE_MS = 60 * 60 * 1000;

/** The small part of `Storage` that these helpers use. */
export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/**
 * Makes a verifier, keeps it with the time in `store` (localStorage, because the sign-in link
 * can open in a new tab), and returns the S256 challenge to send to the auth server.
 */
export async function newPkce(store: KeyValueStore, now: number = Date.now()): Promise<string> {
  const verifier = base64url(crypto.getRandomValues(new Uint8Array(32)));
  store.setItem(PKCE_KEY, JSON.stringify({ verifier, created: now }));
  return challengeFor(verifier);
}

/**
 * Takes the saved verifier out of `store`. It is single use: the entry is removed also when
 * it is too old. Returns `null` if there is none, it is too old, or it is not valid.
 */
export function takeVerifier(store: KeyValueStore, now: number = Date.now()): string | null {
  const raw = store.getItem(PKCE_KEY);
  store.removeItem(PKCE_KEY);
  if (raw === null) return null;
  try {
    const saved = JSON.parse(raw) as { verifier?: unknown; created?: unknown };
    if (typeof saved.verifier !== "string" || typeof saved.created !== "number") return null;
    if (now - saved.created > PKCE_MAX_AGE_MS) return null;
    return saved.verifier;
  } catch {
    return null;
  }
}
