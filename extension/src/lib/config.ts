// The API address. The build sets the default (build.mjs, SAMA_API_URL). The user can change it
// in the panel; the extension then asks for access to that address.

declare const __SAMA_API_URL__: string;

export const API_URL_KEY = "sama.apiUrl";

export function defaultApiUrl(): string {
  return typeof __SAMA_API_URL__ === "string" ? __SAMA_API_URL__ : "https://sama-staging.fly.dev";
}

/** The address without a trailing slash. Throws for an address that is not https (or a local http). */
export function normalizeApiUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error("Type a full address, for example https://sama.example.com.");
  }
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new Error("The address must start with https:// (http:// only for localhost).");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("Type only the address, without a user, a query, or a fragment.");
  }
  return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
}
