// The page to open after sign-in (M8.1). A link that a signed-out tab opens (for example a link
// from the chat) is kept here while the user signs in, then the login page opens it. It is in
// localStorage for at most one hour, because an emailed sign-in link opens in another tab.

import type { KeyValueStore } from "./pkce";

export const RETURN_KEY = "sama.returnTo";
export const RETURN_MAX_AGE_MS = 60 * 60 * 1000;
const MAX_PATH = 2000;

/** A path of this app: it starts with one `/`, is not the login page, and has no scheme. */
export function safeReturnPath(path: string): string | null {
  if (!path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return null;
  if (path.length > MAX_PATH || /[\u0000-\u001f]/.test(path)) return null;
  const bare = path.split(/[?#]/, 1)[0] ?? "";
  if (bare === "/" || bare === "/login" || bare.startsWith("/login/")) return null;
  return path;
}

export function saveReturnTo(store: KeyValueStore, path: string, now: number = Date.now()): void {
  const safe = safeReturnPath(path);
  if (safe === null) return;
  store.setItem(RETURN_KEY, JSON.stringify({ path: safe, at: now }));
}

/** Takes the saved path out of `store` (single use). `null` when none, too old, or not safe. */
export function takeReturnTo(store: KeyValueStore, now: number = Date.now()): string | null {
  const raw = store.getItem(RETURN_KEY);
  if (raw === null) return null;
  store.removeItem(RETURN_KEY);
  try {
    const saved = JSON.parse(raw) as { path?: unknown; at?: unknown };
    if (typeof saved.path !== "string" || typeof saved.at !== "number") return null;
    if (now - saved.at > RETURN_MAX_AGE_MS || saved.at > now + 60_000) return null;
    return safeReturnPath(saved.path);
  } catch {
    return null;
  }
}
