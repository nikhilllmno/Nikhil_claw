// A client of the sama API for one user. The tests use it to set up data, to check what the
// web app did, and for flows that have no page yet (triggers, workflows).

import { type JsonResponse, type RequestOptions, requestJson } from "./http.ts";

export interface MeOrg {
  id: string;
  name: string;
  slug: string;
  user_id: string;
  role: string;
  admin: boolean;
  plan: string;
  workspaces?: { id: string; name: string }[];
  agent_profiles?: { id: string; name: string; model: string; workspace_id: string | null }[];
}

export interface NewOrgOut {
  id: string;
  name: string;
  slug: string;
  plan: string;
  user_id: string;
  workspace_id: string;
  agent_profile_id: string;
}

export interface SessionOut {
  id: string;
  org_id: string;
  workspace_id: string;
  agent_profile_id: string;
  audience: string;
  title: string | null;
  status: string;
}

export interface RunOut {
  id: string;
  status: string;
  kind?: string;
  failure_code?: string | null;
  failure_message?: string | null;
}

export interface MessageOut {
  id: number | string;
  role: string;
  content: unknown;
  text?: string;
}

export const TERMINAL_RUN = new Set(["completed", "failed", "cancelled"]);

/** A function that returns a fresh access token. */
export type TokenFn = () => Promise<string>;

export class SamaApi {
  readonly baseUrl: string;
  private readonly token: TokenFn;

  constructor(baseUrl: string, token: TokenFn) {
    this.baseUrl = baseUrl;
    this.token = token;
  }

  /** A fresh access token, for a request that is not JSON (a file download). */
  async accessToken(): Promise<string> {
    return this.token();
  }

  async call<T>(path: string, options: RequestOptions = {}): Promise<JsonResponse<T>> {
    const headers = { authorization: `Bearer ${await this.token()}`, ...options.headers };
    return requestJson<T>(`${this.baseUrl}${path}`, { ...options, headers });
  }

  async get<T>(path: string): Promise<T> {
    return (await this.call<T>(path)).data;
  }

  async post<T>(path: string, body: unknown = {}): Promise<T> {
    return (await this.call<T>(path, { method: "POST", body })).data;
  }

  async put<T>(path: string, body: unknown): Promise<T> {
    return (await this.call<T>(path, { method: "PUT", body })).data;
  }

  async patch<T>(path: string, body: unknown): Promise<T> {
    return (await this.call<T>(path, { method: "PATCH", body })).data;
  }

  async delete(path: string, allow: number[] = [404]): Promise<number> {
    return (await this.call(path, { method: "DELETE", allow })).status;
  }

  async me(): Promise<{ orgs: MeOrg[] }> {
    return this.get("/me");
  }

  async createOrg(name: string): Promise<NewOrgOut> {
    return this.post("/orgs", { name });
  }

  /** Removes the org and all its data (Section 17.4). The confirm value is the slug. */
  async deleteOrg(orgId: string, slug: string): Promise<number> {
    return this.delete(`/orgs/${orgId}?confirm=${encodeURIComponent(slug)}`);
  }

  async createSession(body: {
    workspace_id: string;
    agent_profile_id: string;
    audience?: "private" | "shared";
    title?: string;
  }): Promise<SessionOut> {
    return this.post("/sessions", body);
  }

  async sendMessage(sessionId: string, text: string): Promise<{ run_id: string; queued: boolean }> {
    return this.post(`/sessions/${sessionId}/messages`, { text });
  }

  async runs(sessionId: string): Promise<RunOut[]> {
    const data = await this.get<RunOut[] | { runs: RunOut[] }>(`/sessions/${sessionId}/runs`);
    return Array.isArray(data) ? data : data.runs;
  }

  async messages(sessionId: string): Promise<MessageOut[]> {
    const data = await this.get<MessageOut[] | { messages: MessageOut[] }>(
      `/sessions/${sessionId}/messages?limit=500`,
    );
    return Array.isArray(data) ? data : data.messages;
  }

  async sessions(): Promise<SessionOut[]> {
    const data = await this.get<SessionOut[] | { sessions: SessionOut[] }>("/sessions");
    return Array.isArray(data) ? data : data.sessions;
  }
}

/** The text of a stored message, whatever shape its content has. */
export function messageText(message: MessageOut): string {
  if (typeof message.text === "string") return message.text;
  const content = message.content;
  if (typeof content === "string") return content;
  return JSON.stringify(content ?? "");
}

/** Polls `check` until it returns a value that is not undefined, or the time is up. */
export async function poll<T>(
  what: string,
  check: () => Promise<T | undefined>,
  { timeoutMs = 120_000, intervalMs = 2_000 } = {},
): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  let last: unknown;
  for (;;) {
    try {
      const value = await check();
      if (value !== undefined) return value;
    } catch (error) {
      last = error;
    }
    if (Date.now() > deadline) {
      const reason = last instanceof Error ? `: ${last.message}` : "";
      throw new Error(`Timed out after ${timeoutMs} ms waiting for ${what}${reason}`);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

/** Waits until the session has `count` runs or more and all of them have ended. */
export async function waitForRuns(
  api: SamaApi,
  sessionId: string,
  { count = 1, timeoutMs = 240_000 } = {},
): Promise<RunOut[]> {
  return poll(
    `${count} ended run(s) in session ${sessionId}`,
    async () => {
      const runs = await api.runs(sessionId);
      if (runs.length >= count && runs.every((run) => TERMINAL_RUN.has(run.status))) return runs;
      return undefined;
    },
    { timeoutMs, intervalMs: 3_000 },
  );
}
