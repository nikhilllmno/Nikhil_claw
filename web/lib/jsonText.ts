// Parse a text field that holds a JSON object (a test payload, a config, the inputs of a
// workflow). The screens use it, and the API checks the same limits again.

export type JsonObjectResult =
  | { ok: true; /** `null`: the field is empty. */ value: Record<string, unknown> | null }
  | { ok: false; error: string };

/**
 * The object in `text`. An empty text gives `{ ok: true, value: null }`. `what` is the noun
 * phrase for the messages, for example "The payload". `maxBytes` limits the size of the JSON.
 */
export function parseJsonObject(text: string, what: string, maxBytes: number): JsonObjectResult {
  const trimmed = text.trim();
  if (trimmed === "") return { ok: true, value: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: `${what} is not valid JSON.` };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { ok: false, error: `${what} must be a JSON object, for example {"key": "value"}.` };
  }
  const size = new TextEncoder().encode(JSON.stringify(parsed)).length;
  if (size > maxBytes) {
    return { ok: false, error: `${what} has more than ${maxBytes.toLocaleString("en-US")} bytes.` };
  }
  return { ok: true, value: parsed as Record<string, unknown> };
}

/** The text of a value in a read-only box: JSON with an indent. */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return String(value);
  }
}
