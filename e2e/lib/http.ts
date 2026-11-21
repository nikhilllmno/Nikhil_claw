// A small JSON client over fetch. Errors carry the status and a short part of the body, never
// the request headers, so a token cannot reach a test log.

export class HttpError extends Error {
  readonly method: string;
  readonly url: string;
  readonly status: number;
  readonly body: string;

  constructor(method: string, url: string, status: number, body: string) {
    super(`${method} ${redact(url)} answered ${status}: ${body.slice(0, 500)}`);
    this.name = "HttpError";
    this.method = method;
    this.url = url;
    this.status = status;
    this.body = body;
  }
}

/** The URL without its query, because a query can have a token. */
export function redact(url: string): string {
  const index = url.indexOf("?");
  return index < 0 ? url : `${url.slice(0, index)}?…`;
}

export interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
  /** Return these statuses instead of throwing. */
  allow?: number[];
}

export interface JsonResponse<T> {
  status: number;
  data: T;
  headers: Headers;
}

export async function requestJson<T>(url: string, options: RequestOptions = {}): Promise<JsonResponse<T>> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = { "user-agent": "sama-e2e", ...options.headers };
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(url, { method, headers, body });
  const text = await response.text();
  if (!response.ok && !(options.allow ?? []).includes(response.status)) {
    throw new HttpError(method, url, response.status, text);
  }
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  return { status: response.status, data: data as T, headers: response.headers };
}
