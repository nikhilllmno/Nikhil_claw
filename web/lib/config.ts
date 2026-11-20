// The one setting of the app: the address of the sama API.

/** The API address without a trailing slash. Empty if the variable is not set. */
export function apiUrl(): string {
  return (process.env.NEXT_PUBLIC_SAMA_API_URL ?? "").trim().replace(/\/+$/, "");
}
