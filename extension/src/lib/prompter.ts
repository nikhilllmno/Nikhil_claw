// The questions of the extension to the user (M5.5): "Let sama act on this site?" and "Let sama
// do this sensitive action?". The service worker keeps the open questions. The side panel shows
// them; when no panel is open, the service worker opens a small sama window (prompt.html). Only
// a click in an extension page answers a question: a web page cannot answer it.

import type { ActionAnswer, Prompter, SiteAnswer } from "./actions";
import type { Prompt, PromptChoice } from "./messages";

interface Open {
  prompt: Prompt;
  resolve: (choice: PromptChoice | "timeout") => void;
  timer: unknown;
}

export interface PromptDeps {
  /** Tells the open pages that the questions changed. */
  publish(prompts: Prompt[]): void;
  /** True when a side panel or a sama window can show the questions. */
  hasViewer(): boolean;
  /** Opens the sama window. */
  openWindow(): void;
  newId?: () => string;
  now?: () => number;
}

export class Prompts implements Prompter {
  private readonly open = new Map<string, Open>();
  private readonly newId: () => string;
  private readonly now: () => number;

  constructor(private readonly deps: PromptDeps) {
    this.newId = deps.newId ?? (() => crypto.randomUUID());
    this.now = deps.now ?? (() => Date.now());
  }

  list(): Prompt[] {
    return [...this.open.values()].map((entry) => entry.prompt);
  }

  private ask(fields: Omit<Prompt, "id" | "expiresAt">, waitMs: number): Promise<PromptChoice | "timeout"> {
    if (waitMs <= 0) return Promise.resolve("timeout");
    const id = this.newId();
    const prompt: Prompt = { ...fields, id, expiresAt: this.now() + waitMs };
    return new Promise((resolve) => {
      const timer = setTimeout(() => this.settle(id, "timeout"), waitMs);
      this.open.set(id, { prompt, resolve, timer });
      this.deps.publish(this.list());
      if (!this.deps.hasViewer()) this.deps.openWindow();
    });
  }

  private settle(id: string, choice: PromptChoice | "timeout"): boolean {
    const entry = this.open.get(id);
    if (entry === undefined) return false;
    this.open.delete(id);
    clearTimeout(entry.timer as ReturnType<typeof setTimeout>);
    entry.resolve(choice);
    this.deps.publish(this.list());
    return true;
  }

  /** The answer of the user. Returns false for a question that is not open. */
  answer(id: string, choice: PromptChoice): boolean {
    const entry = this.open.get(id);
    if (entry === undefined) return false;
    // "once" and "never" are answers to a site question only.
    if (entry.prompt.kind === "action" && choice !== "allow" && choice !== "deny") return false;
    return this.settle(id, choice);
  }

  async site(request: { runId: string; tabId: number; site: string }, waitMs: number): Promise<SiteAnswer> {
    return this.ask({ kind: "site", reason: null, label: null, ...request }, waitMs);
  }

  async action(
    request: { runId: string; tabId: number; site: string; reason: string; label: string },
    waitMs: number,
  ): Promise<ActionAnswer> {
    const label = request.label.replace(/\s+/g, " ").trim().slice(0, 120);
    const choice = await this.ask({ kind: "action", ...request, label }, waitMs);
    return choice === "allow" || choice === "timeout" ? choice : "deny";
  }

  drop(runId: string): void {
    for (const [id, entry] of this.open) if (entry.prompt.runId === runId) this.settle(id, "deny");
  }
}
