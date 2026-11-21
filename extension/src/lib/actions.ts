// The `my_chrome` browser target in the extension (docs/architecture.md, Section 14.2.2, M5.5).
//
// The API sends a `tool_call` for each browser call of a run. The `Actor` does the call in the
// tabs of the run with chrome.debugger (CDP 1.3) and answers with a `tool_result`.
//
// - The tabs of a run: the tab that is bound to the session of the run, and the tabs that the
//   agent opens (or that a page of the run opens). The agent acts in no other tab.
// - Consent: before the agent reads or acts on a site, the site must be allowed. The user is asked
//   once for each site (allow on this site, allow once, not now, never). Blocked sites are refused.
// - Sensitive actions: the extension checks the live element. A sensitive action that no person
//   approved in the approval card waits for a click of the user.
// - The overlay shows the cursor and a Stop button. A click or a key press of the user pauses the
//   agent ("you have control"). Stop cancels the run and detaches.

import { hostOf, isBlockedSite, isWebPage } from "./blocklist";
import { buildTree, type AXNode, domFacts, type DomNode } from "./axtree";
import type { SiteConsent } from "./consent";
import { extractFrom, fieldValue, selectAllText, selectOptions } from "./extract";
import { parseKey } from "./keys";
import { installOverlay, OVERLAY_BINDING, OVERLAY_WORLD, type OverlayMode, type OverlaySignal } from "./overlay";
import type { ToolCallFrame, ToolErrorCode, ToolResultFrame } from "./protocol";
import { actionReason, elementFacts, type ElementFacts, REASON_TEXT, type SensitiveCode } from "./sensitive";

/** The longest wait for the user: a consent, a sensitive action, or the end of a takeover. */
export const USER_WAIT_MS = 120_000;
export const LOAD_WAIT_MS = 20_000;
export const SETTLE_MS = 500;
export const CURSOR_MS = 250;
export const MAX_TABS = 10;
export const MAX_SCREENSHOT_CHARS = 5_300_000;
export const MAX_FULL_PAGE_PX = 6_000;

export class ToolFailure extends Error {
  constructor(
    readonly code: ToolErrorCode,
    message = "",
  ) {
    super(message);
  }
}

/** The part of chrome.debugger that the actor uses (tests give a fake). */
export interface Cdp {
  attach(tabId: number): Promise<void>;
  detach(tabId: number): Promise<void>;
  send<T = unknown>(tabId: number, method: string, params?: Record<string, unknown>): Promise<T>;
}

export interface TabInfo {
  id: number;
  url: string;
  title: string;
  status: string;
  windowId: number;
}

/** The part of chrome.tabs that the actor uses. */
export interface Tabs {
  get(tabId: number): Promise<TabInfo | null>;
  create(options: { url: string; windowId?: number; openerTabId?: number }): Promise<TabInfo>;
  update(tabId: number, options: { url?: string; active?: boolean }): Promise<void>;
  remove(tabId: number): Promise<void>;
}

export type SiteAnswer = "allow" | "once" | "deny" | "never" | "timeout";
export type ActionAnswer = "allow" | "deny" | "timeout";

/** Asks the user (the side panel or the sama window). */
export interface Prompter {
  site(request: { runId: string; tabId: number; site: string }, waitMs: number): Promise<SiteAnswer>;
  action(
    request: { runId: string; tabId: number; site: string; reason: string; label: string },
    waitMs: number,
  ): Promise<ActionAnswer>;
  /** Closes the open prompts of a run (the run stopped). */
  drop(runId: string): void;
}

export interface ActorDeps {
  cdp: Cdp;
  tabs: Tabs;
  consent: SiteConsent;
  prompter: Prompter;
  /** The tab that is bound to a session, or `null`. */
  boundTab(sessionId: string): Promise<number | null>;
  /** Cancels a run on the API (the Stop button). */
  cancelRun(runId: string): Promise<void>;
  /** The agent state of the tabs changed (the side panel shows it). */
  onChange(): void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

interface RunState {
  runId: string;
  sessionId: string;
  tabs: number[];
  /** The tab that the agent opened itself (it can close only these). */
  opened: Set<number>;
  active: number | null;
  /** The sites that the user allowed for this run only. */
  once: Set<string>;
  stopped: boolean;
  notes: string[];
}

interface TabState {
  tabId: number;
  runId: string;
  attached: boolean;
  world: number | null;
  paused: boolean;
  mode: OverlayMode;
  refs: Map<string, number>;
  dialog: { kind: string; message: string } | null;
  resumed: (() => void)[];
  dialogWaiters: (() => void)[];
}

/** The state of a tab for the side panel. */
export interface AgentTab {
  tabId: number;
  runId: string;
  sessionId: string;
  paused: boolean;
}

interface Quad {
  x: number;
  y: number;
  rect: { x: number; y: number; width: number; height: number };
}

const DIALOG = Symbol("dialog");

export class Actor {
  private readonly runs = new Map<string, RunState>();
  private readonly tabStates = new Map<number, TabState>();
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;

  constructor(private readonly deps: ActorDeps) {
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.now = deps.now ?? (() => Date.now());
  }

  /** The tabs where the agent acts now, for the side panel. */
  agentTabs(): AgentTab[] {
    const out: AgentTab[] = [];
    for (const tab of this.tabStates.values()) {
      const run = this.runs.get(tab.runId);
      if (run && tab.attached && !run.stopped) {
        out.push({ tabId: tab.tabId, runId: run.runId, sessionId: run.sessionId, paused: tab.paused });
      }
    }
    return out;
  }

  // -- the calls of the API ---------------------------------------------------------------------

  async handle(frame: ToolCallFrame): Promise<ToolResultFrame> {
    const deadline = this.now() + Math.max(1_000, frame.timeout_ms);
    try {
      const result = await this.run(frame, deadline);
      return { type: "tool_result", call_id: frame.call_id, ok: true, result };
    } catch (error) {
      const failure =
        error instanceof ToolFailure
          ? error
          : new ToolFailure("failed", error instanceof Error ? error.message.slice(0, 500) : "");
      return {
        type: "tool_result",
        call_id: frame.call_id,
        ok: false,
        error: { code: failure.code, message: failure.message.slice(0, 1_000) },
      };
    }
  }

  private async run(frame: ToolCallFrame, deadline: number): Promise<Record<string, unknown>> {
    if (frame.op === "release") {
      await this.release(frame.run_id);
      return {};
    }
    const run = await this.runOf(frame);
    const args = frame.args;
    switch (frame.op) {
      case "navigate":
        return this.navigate(run, String(args.url ?? ""), deadline);
      case "snapshot":
        return this.snapshot(run, deadline);
      case "act":
        return this.act(run, args, frame.approved, deadline);
      case "screenshot":
        return this.screenshot(run, args.full_page === true, deadline);
      case "extract":
        return this.extract(run, args, deadline);
      case "tabs":
        return this.tabs(run, args, deadline);
      default:
        throw new ToolFailure("invalid", "unknown operation");
    }
  }

  private async runOf(frame: ToolCallFrame): Promise<RunState> {
    let run = this.runs.get(frame.run_id);
    if (run === undefined) {
      run = {
        runId: frame.run_id,
        sessionId: frame.session_id,
        tabs: [],
        opened: new Set(),
        active: null,
        once: new Set(),
        stopped: false,
        notes: [],
      };
      const bound = frame.session_id ? await this.deps.boundTab(frame.session_id) : null;
      if (bound !== null && (await this.deps.tabs.get(bound)) !== null) {
        const owner = this.tabStates.get(bound)?.runId;
        if (owner !== undefined && owner !== frame.run_id && this.runs.get(owner)?.stopped === false) {
          throw new ToolFailure("busy");
        }
        run.tabs.push(bound);
        run.active = bound;
      }
      this.runs.set(frame.run_id, run);
    }
    if (run.stopped) throw new ToolFailure("stopped");
    return run;
  }

  // -- consent and control ----------------------------------------------------------------------

  private remaining(deadline: number): number {
    return Math.max(0, Math.min(USER_WAIT_MS, deadline - this.now()));
  }

  /** Checks the site of `url` for the run. It asks the user when the site has no choice. */
  private async checkSite(run: RunState, tabId: number, url: string, deadline: number): Promise<void> {
    if (!isWebPage(url)) throw new ToolFailure("not_web");
    const state = await this.deps.consent.state(url);
    if (state === "blocked") throw new ToolFailure("blocked");
    if (state === "never") throw new ToolFailure("never");
    const site = hostOf(url) ?? "";
    if (state === "allow" || run.once.has(site)) return;
    await this.setMode(tabId, "waiting");
    const answer = await this.deps.prompter.site({ runId: run.runId, tabId, site }, this.remaining(deadline));
    await this.setMode(tabId, "acting");
    if (run.stopped) throw new ToolFailure("stopped");
    switch (answer) {
      case "allow":
        await this.deps.consent.set(url, "allow");
        run.notes.push("consent");
        return;
      case "once":
        run.once.add(site);
        run.notes.push("consent");
        return;
      case "never":
        await this.deps.consent.set(url, "never");
        throw new ToolFailure("never");
      case "timeout":
        throw new ToolFailure("consent_timeout");
      default:
        throw new ToolFailure("consent_denied");
    }
  }

  private activeTab(run: RunState): number {
    if (run.active === null) throw new ToolFailure("no_tab");
    return run.active;
  }

  private async tabInfo(tabId: number): Promise<TabInfo> {
    const info = await this.deps.tabs.get(tabId);
    if (info === null) throw new ToolFailure("no_tab");
    return info;
  }

  /** Checks the site of the active tab, attaches the debugger, and waits while the user has
   * control. Returns the tab state. */
  private async prepare(run: RunState, deadline: number): Promise<TabState> {
    const tabId = this.activeTab(run);
    const info = await this.tabInfo(tabId);
    await this.checkSite(run, tabId, info.url, deadline);
    const tab = await this.attach(run, tabId);
    await this.ensureWorld(tab);
    if (tab.paused) await this.waitForResume(run, tab, deadline);
    return tab;
  }

  private async attach(run: RunState, tabId: number): Promise<TabState> {
    let tab = this.tabStates.get(tabId);
    if (tab !== undefined && tab.runId !== run.runId) {
      if (this.runs.get(tab.runId)?.stopped === false) throw new ToolFailure("busy");
      this.tabStates.delete(tabId);
      tab = undefined;
    }
    if (tab === undefined) {
      tab = {
        tabId,
        runId: run.runId,
        attached: false,
        world: null,
        paused: false,
        mode: "acting",
        refs: new Map(),
        dialog: null,
        resumed: [],
        dialogWaiters: [],
      };
      this.tabStates.set(tabId, tab);
    }
    if (!tab.attached) {
      try {
        await this.deps.cdp.attach(tabId);
      } catch (error) {
        throw new ToolFailure("debugger", error instanceof Error ? error.message : "");
      }
      tab.attached = true;
      tab.world = null;
      await this.deps.cdp.send(tabId, "Page.enable");
      await this.deps.cdp.send(tabId, "Runtime.enable");
      await this.deps.cdp.send(tabId, "Runtime.addBinding", {
        name: OVERLAY_BINDING,
        executionContextName: OVERLAY_WORLD,
      });
      this.deps.onChange();
    }
    return tab;
  }

  /** The isolated world of the overlay in the main frame, made again after a navigation. */
  private async ensureWorld(tab: TabState): Promise<number> {
    if (tab.world !== null) {
      try {
        const check = await this.deps.cdp.send<{ result?: { value?: unknown } }>(tab.tabId, "Runtime.evaluate", {
          contextId: tab.world,
          expression: "typeof globalThis.__sama === 'object'",
          returnByValue: true,
        });
        if (check.result?.value === true) return tab.world;
      } catch {
        // The world went away with a navigation.
      }
    }
    const frames = await this.deps.cdp.send<{ frameTree: { frame: { id: string } } }>(tab.tabId, "Page.getFrameTree");
    const world = await this.deps.cdp.send<{ executionContextId: number }>(tab.tabId, "Page.createIsolatedWorld", {
      frameId: frames.frameTree.frame.id,
      worldName: OVERLAY_WORLD,
      grantUniveralAccess: false,
    });
    tab.world = world.executionContextId;
    await this.deps.cdp.send(tab.tabId, "Runtime.evaluate", {
      contextId: tab.world,
      expression: `(${installOverlay.toString()})(${JSON.stringify(OVERLAY_BINDING)})`,
      returnByValue: true,
    });
    await this.overlay(tab, `mode(${JSON.stringify(tab.paused ? "paused" : tab.mode)})`);
    return tab.world;
  }

  /** Calls a function of the overlay. It never fails the call. */
  private async overlay(tab: TabState, call: string): Promise<void> {
    if (!tab.attached || tab.world === null) return;
    try {
      await this.deps.cdp.send(tab.tabId, "Runtime.evaluate", {
        contextId: tab.world,
        expression: `globalThis.__sama?.${call}`,
        returnByValue: true,
      });
    } catch {
      // The page changed; the next call makes the overlay again.
    }
  }

  private async setMode(tabId: number, mode: OverlayMode): Promise<void> {
    const tab = this.tabStates.get(tabId);
    if (tab === undefined) return;
    tab.mode = mode;
    await this.overlay(tab, `mode(${JSON.stringify(tab.paused ? "paused" : mode)})`);
  }

  private async waitForResume(run: RunState, tab: TabState, deadline: number): Promise<void> {
    const wait = this.remaining(deadline);
    const resumed = await new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), wait);
      tab.resumed.push(() => {
        clearTimeout(timer);
        resolve(true);
      });
    });
    if (run.stopped) throw new ToolFailure("stopped");
    if (!resumed || tab.paused) throw new ToolFailure("taken_over");
  }

  /** A signal of the overlay in a tab: Stop, Give back, or a takeover by the user. */
  async signal(tabId: number, signal: OverlaySignal): Promise<void> {
    const tab = this.tabStates.get(tabId);
    if (tab === undefined) return;
    if (signal === "stop") {
      await this.stop(tab.runId);
    } else if (signal === "takeover") {
      if (tab.paused) return;
      tab.paused = true;
      await this.overlay(tab, `mode("paused")`);
      this.deps.onChange();
    } else {
      this.resume(tab);
    }
  }

  /** The user gives the tab back (the overlay or the side panel). */
  resumeTab(tabId: number): void {
    const tab = this.tabStates.get(tabId);
    if (tab !== undefined) this.resume(tab);
  }

  private resume(tab: TabState): void {
    if (!tab.paused) return;
    tab.paused = false;
    void this.overlay(tab, `mode(${JSON.stringify(tab.mode)})`);
    for (const done of tab.resumed.splice(0)) done();
    this.deps.onChange();
  }

  /** Stops a run: the open prompts close, the API cancels the run, and the tabs detach. */
  async stop(runId: string): Promise<void> {
    const run = this.runs.get(runId);
    if (run === undefined || run.stopped) return;
    run.stopped = true;
    this.deps.prompter.drop(runId);
    for (const tabId of run.tabs) {
      const tab = this.tabStates.get(tabId);
      if (tab !== undefined) for (const done of tab.resumed.splice(0)) done();
    }
    await this.detachRun(run);
    this.deps.onChange();
    await this.deps.cancelRun(runId).catch(() => undefined);
  }

  /** Stops the run that acts in a tab (the Stop button of the side panel). */
  async stopTab(tabId: number): Promise<void> {
    const tab = this.tabStates.get(tabId);
    if (tab !== undefined) await this.stop(tab.runId);
  }

  /** Releases every run (sign-out or a new API address). */
  async releaseAll(): Promise<void> {
    for (const runId of [...this.runs.keys()]) await this.release(runId);
  }

  private async release(runId: string): Promise<void> {
    const run = this.runs.get(runId);
    if (run === undefined) return;
    this.runs.delete(runId);
    this.deps.prompter.drop(runId);
    await this.detachRun(run);
    this.deps.onChange();
  }

  private async detachRun(run: RunState): Promise<void> {
    for (const tabId of run.tabs) {
      const tab = this.tabStates.get(tabId);
      if (tab === undefined || tab.runId !== run.runId) continue;
      await this.overlay(tab, "remove()");
      if (tab.attached) await this.deps.cdp.detach(tabId).catch(() => undefined);
      this.tabStates.delete(tabId);
    }
  }

  // -- the events of Chrome ---------------------------------------------------------------------

  /** A CDP event of an attached tab. */
  onEvent(tabId: number, method: string, params: Record<string, unknown>): void {
    const tab = this.tabStates.get(tabId);
    if (tab === undefined) return;
    if (method === "Runtime.bindingCalled" && params.name === OVERLAY_BINDING) {
      if (params.executionContextId !== tab.world) return;
      const payload = params.payload;
      if (payload === "stop" || payload === "resume" || payload === "takeover") void this.signal(tabId, payload);
    } else if (method === "Page.javascriptDialogOpening") {
      tab.dialog = { kind: String(params.type ?? "alert"), message: String(params.message ?? "") };
      for (const done of tab.dialogWaiters.splice(0)) done();
    } else if (method === "Page.javascriptDialogClosed") {
      tab.dialog = null;
    } else if (method === "Runtime.executionContextsCleared") {
      tab.world = null;
    }
  }

  /** Chrome detached the debugger: the user closed the bar of Chrome, or the tab closed. */
  onDetach(tabId: number, reason: string): void {
    const tab = this.tabStates.get(tabId);
    if (tab === undefined) return;
    tab.attached = false;
    tab.world = null;
    if (reason === "canceled_by_user") void this.stop(tab.runId);
    this.deps.onChange();
  }

  /** A new tab. A tab that a page of a run opens joins the run and becomes its active tab. */
  onTabCreated(tabId: number, openerTabId: number | undefined): void {
    if (openerTabId === undefined) return;
    for (const run of this.runs.values()) {
      if (run.stopped || run.active !== openerTabId || run.tabs.includes(tabId)) continue;
      if (run.tabs.length >= MAX_TABS) return;
      run.tabs.push(tabId);
      run.opened.add(tabId);
      run.active = tabId;
      run.notes.push("new_tab");
      return;
    }
  }

  onTabRemoved(tabId: number): void {
    const tab = this.tabStates.get(tabId);
    this.tabStates.delete(tabId);
    for (const run of this.runs.values()) {
      const index = run.tabs.indexOf(tabId);
      if (index < 0) continue;
      run.tabs.splice(index, 1);
      run.opened.delete(tabId);
      if (run.active === tabId) {
        run.active = run.tabs.at(-1) ?? null;
        run.notes.push("tab_closed");
      }
    }
    if (tab !== undefined) this.deps.onChange();
  }

  // -- page state -------------------------------------------------------------------------------

  private async page(run: RunState): Promise<Record<string, unknown>> {
    const notes = run.notes.splice(0);
    if (run.active === null) return { url: "", title: "", tab: 0, tabs: run.tabs.length, notes };
    const info = await this.deps.tabs.get(run.active);
    const dialog = this.tabStates.get(run.active)?.dialog;
    if (dialog) notes.push("dialog_open");
    return {
      url: info?.url ?? "",
      title: info?.title ?? "",
      tab: Math.max(0, run.tabs.indexOf(run.active)),
      tabs: run.tabs.length,
      notes,
    };
  }

  private async waitForLoad(tabId: number, deadline: number): Promise<void> {
    const end = Math.min(deadline, this.now() + LOAD_WAIT_MS);
    while (this.now() < end) {
      const info = await this.deps.tabs.get(tabId);
      if (info === null || info.status === "complete") return;
      await this.sleep(100);
    }
  }

  // -- the operations ---------------------------------------------------------------------------

  private async navigate(run: RunState, url: string, deadline: number): Promise<Record<string, unknown>> {
    if (!isWebPage(url)) throw new ToolFailure("not_web");
    if (isBlockedSite(url)) throw new ToolFailure("blocked");
    if (run.active === null) {
      await this.checkSite(run, -1, url, deadline);
      const created = await this.deps.tabs.create({ url });
      run.tabs.push(created.id);
      run.opened.add(created.id);
      run.active = created.id;
    } else {
      const tabId = run.active;
      await this.checkSite(run, tabId, url, deadline);
      const tab = this.tabStates.get(tabId);
      if (tab?.paused) await this.waitForResume(run, tab, deadline);
      await this.deps.tabs.update(tabId, { url, active: true });
      if (tab) tab.world = null;
      await this.sleep(100);
    }
    await this.waitForLoad(run.active, deadline);
    const tab = await this.prepare(run, deadline);
    tab.refs.clear();
    return this.page(run);
  }

  private async snapshot(run: RunState, deadline: number): Promise<Record<string, unknown>> {
    const tab = await this.prepare(run, deadline);
    if (tab.dialog !== null) {
      return { page: await this.page(run), tree: "", dialog: tab.dialog, sensitive: {}, submits: [] };
    }
    const document = await this.deps.cdp.send<{ root: DomNode }>(tab.tabId, "DOM.getDocument", { depth: -1, pierce: true });
    const facts = domFacts(document.root);
    const ax = await this.deps.cdp.send<{ nodes: AXNode[] }>(tab.tabId, "Accessibility.getFullAXTree");
    const built = buildTree(ax.nodes, (id) => facts.get(id) ?? null);
    tab.refs = built.refs;
    return { page: await this.page(run), tree: built.tree, sensitive: built.sensitive, submits: built.submits };
  }

  private nodeOf(tab: TabState, ref: unknown): number {
    const key = typeof ref === "string" ? ref.replace(/^ref=/, "").trim() : "";
    const node = tab.refs.get(key);
    if (node === undefined) throw new ToolFailure("stale_ref");
    return node;
  }

  /** The object of a node in the isolated world. */
  private async objectOf(tab: TabState, backendNodeId: number): Promise<string> {
    const world = await this.ensureWorld(tab);
    try {
      const resolved = await this.deps.cdp.send<{ object: { objectId?: string } }>(tab.tabId, "DOM.resolveNode", {
        backendNodeId,
        executionContextId: world,
      });
      if (!resolved.object.objectId) throw new Error("no object");
      return resolved.object.objectId;
    } catch {
      throw new ToolFailure("stale_ref");
    }
  }

  private async callOn<T>(tab: TabState, objectId: string, fn: (...args: never[]) => unknown, args: unknown[] = []): Promise<T> {
    const answer = await this.deps.cdp.send<{ result?: { value?: unknown }; exceptionDetails?: { exception?: { description?: string } } }>(
      tab.tabId,
      "Runtime.callFunctionOn",
      {
        objectId,
        functionDeclaration: fn.toString(),
        arguments: args.map((value) => ({ value })),
        returnByValue: true,
        awaitPromise: true,
      },
    );
    if (answer.exceptionDetails) {
      throw new ToolFailure("failed", answer.exceptionDetails.exception?.description?.split("\n")[0] ?? "");
    }
    return answer.result?.value as T;
  }

  private async focusedObject(tab: TabState): Promise<string | null> {
    const world = await this.ensureWorld(tab);
    const answer = await this.deps.cdp.send<{ result?: { objectId?: string; subtype?: string } }>(tab.tabId, "Runtime.evaluate", {
      contextId: world,
      expression: "document.activeElement",
    });
    return answer.result?.subtype === "node" && answer.result.objectId ? answer.result.objectId : null;
  }

  /** The center of a node in the viewport, after a scroll into view. */
  private async pointOf(tab: TabState, backendNodeId: number): Promise<Quad> {
    try {
      await this.deps.cdp.send(tab.tabId, "DOM.scrollIntoViewIfNeeded", { backendNodeId });
    } catch {
      // Not all nodes can scroll; the quads tell.
    }
    const quads = await this.deps.cdp
      .send<{ quads: number[][] }>(tab.tabId, "DOM.getContentQuads", { backendNodeId })
      .then((answer) => answer.quads)
      .catch(() => {
        throw new ToolFailure("stale_ref");
      });
    const quad = quads.find((item) => item.length === 8);
    if (quad === undefined) throw new ToolFailure("failed", "the element is not visible");
    const xs = [quad[0], quad[2], quad[4], quad[6]] as number[];
    const ys = [quad[1], quad[3], quad[5], quad[7]] as number[];
    const left = Math.min(...xs);
    const top = Math.min(...ys);
    const width = Math.max(...xs) - left;
    const height = Math.max(...ys) - top;
    return { x: left + width / 2, y: top + height / 2, rect: { x: left, y: top, width, height } };
  }

  private async showTarget(tab: TabState, point: Quad): Promise<void> {
    await this.overlay(tab, `box(${JSON.stringify(point.rect)})`);
    await this.overlay(tab, `move(${point.x}, ${point.y})`);
    await this.sleep(CURSOR_MS);
  }

  /** Sends input events with the overlay told that the agent acts, so it is not a takeover. A
   * dialog that the input opens ends the wait at once. */
  private async input(tab: TabState, events: [string, Record<string, unknown>][]): Promise<boolean> {
    await this.overlay(tab, "busy(true)");
    try {
      for (const [method, params] of events) {
        const sent = this.deps.cdp.send(tab.tabId, method, params);
        // After a dialog the send ends later; its error then has no reader.
        sent.catch(() => undefined);
        const dialog = new Promise<typeof DIALOG>((resolve) => tab.dialogWaiters.push(() => resolve(DIALOG)));
        if ((await Promise.race([sent, dialog])) === DIALOG) return true;
      }
      return false;
    } finally {
      tab.dialogWaiters.length = 0;
      await this.sleep(30);
      await this.overlay(tab, "busy(false)");
    }
  }

  private mouse(type: string, x: number, y: number, extra: Record<string, unknown> = {}): [string, Record<string, unknown>] {
    return ["Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1, ...extra }];
  }

  private keyEvents(spec: string): [string, Record<string, unknown>][] {
    const key = parseKey(spec);
    if (key === null) throw new ToolFailure("invalid", "the key is not known");
    const base = { key: key.key, code: key.code, windowsVirtualKeyCode: key.keyCode, modifiers: key.modifiers };
    return [
      ["Input.dispatchKeyEvent", { type: key.text ? "keyDown" : "rawKeyDown", ...base, text: key.text || undefined }],
      ["Input.dispatchKeyEvent", { type: "keyUp", ...base }],
    ];
  }

  /** Checks a sensitive action on the live page. Without an approval, it asks the user. */
  private async checkSensitive(
    run: RunState,
    tab: TabState,
    args: Record<string, unknown>,
    approved: boolean,
    deadline: number,
  ): Promise<void> {
    const action = String(args.action ?? "");
    if (!["click", "type", "select", "press"].includes(action)) return;
    let objectId: string | null = null;
    if (typeof args.ref === "string" && args.ref) objectId = await this.objectOf(tab, this.nodeOf(tab, args.ref));
    else if (action === "press") objectId = await this.focusedObject(tab);
    if (objectId === null) return;
    const facts = await this.callOn<ElementFacts>(tab, objectId, elementFacts);
    const reason: SensitiveCode | null = actionReason(
      { action, submit: args.submit === true, key: typeof args.text === "string" ? args.text : null },
      facts,
    );
    if (reason === null || approved) return;
    const info = await this.tabInfo(tab.tabId);
    await this.setMode(tab.tabId, "waiting");
    const answer = await this.deps.prompter.action(
      { runId: run.runId, tabId: tab.tabId, site: hostOf(info.url) ?? "", reason: REASON_TEXT[reason], label: facts.name },
      this.remaining(deadline),
    );
    await this.setMode(tab.tabId, "acting");
    if (run.stopped) throw new ToolFailure("stopped");
    if (answer === "timeout") throw new ToolFailure("sensitive_timeout");
    if (answer !== "allow") throw new ToolFailure("sensitive_denied");
    run.notes.push("approved");
  }

  private async act(
    run: RunState,
    args: Record<string, unknown>,
    approved: boolean,
    deadline: number,
  ): Promise<Record<string, unknown>> {
    const tab = await this.prepare(run, deadline);
    const action = String(args.action ?? "");
    if (tab.dialog !== null && action !== "dialog" && action !== "wait") {
      throw new ToolFailure("failed", "a dialog is open; answer it with action dialog");
    }
    await this.checkSensitive(run, tab, args, approved, deadline);
    if (tab.paused) await this.waitForResume(run, tab, deadline);
    let settle = false;
    let dialog = false;
    switch (action) {
      case "click": {
        const point = await this.pointOf(tab, this.nodeOf(tab, args.ref));
        await this.showTarget(tab, point);
        dialog = await this.input(tab, [
          this.mouse("mouseMoved", point.x, point.y, { button: "none", clickCount: 0 }),
          this.mouse("mousePressed", point.x, point.y),
          this.mouse("mouseReleased", point.x, point.y),
        ]);
        settle = true;
        break;
      }
      case "hover": {
        const point = await this.pointOf(tab, this.nodeOf(tab, args.ref));
        await this.showTarget(tab, point);
        dialog = await this.input(tab, [this.mouse("mouseMoved", point.x, point.y, { button: "none", clickCount: 0 })]);
        break;
      }
      case "type": {
        const node = this.nodeOf(tab, args.ref);
        const point = await this.pointOf(tab, node);
        await this.showTarget(tab, point);
        await this.deps.cdp.send(tab.tabId, "DOM.focus", { backendNodeId: node }).catch(() => {
          throw new ToolFailure("failed", "the element cannot take text");
        });
        await this.callOn(tab, await this.objectOf(tab, node), selectAllText);
        const text = typeof args.text === "string" ? args.text : "";
        const events: [string, Record<string, unknown>][] = text
          ? [["Input.insertText", { text }]]
          : this.keyEvents("Backspace");
        if (args.submit === true) events.push(...this.keyEvents("Enter"));
        dialog = await this.input(tab, events);
        settle = args.submit === true;
        break;
      }
      case "select": {
        const node = this.nodeOf(tab, args.ref);
        await this.showTarget(tab, await this.pointOf(tab, node));
        const values = Array.isArray(args.values) ? args.values.map(String) : [];
        await this.callOn(tab, await this.objectOf(tab, node), selectOptions, [values]);
        break;
      }
      case "press": {
        if (typeof args.ref === "string" && args.ref) {
          await this.deps.cdp.send(tab.tabId, "DOM.focus", { backendNodeId: this.nodeOf(tab, args.ref) }).catch(() => undefined);
        }
        dialog = await this.input(tab, this.keyEvents(typeof args.text === "string" ? args.text : ""));
        settle = true;
        break;
      }
      case "scroll": {
        if (typeof args.ref === "string" && args.ref) {
          await this.pointOf(tab, this.nodeOf(tab, args.ref));
          break;
        }
        const metrics = await this.deps.cdp.send<{ cssLayoutViewport: { clientWidth: number; clientHeight: number } }>(
          tab.tabId,
          "Page.getLayoutMetrics",
        );
        const amount = typeof args.amount === "number" ? args.amount : 600;
        const direction = String(args.direction ?? "down");
        const deltaX = direction === "left" ? -amount : direction === "right" ? amount : 0;
        const deltaY = direction === "up" ? -amount : direction === "down" ? amount : 0;
        const x = metrics.cssLayoutViewport.clientWidth / 2;
        const y = metrics.cssLayoutViewport.clientHeight / 2;
        dialog = await this.input(tab, [["Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX, deltaY }]]);
        await this.sleep(200);
        break;
      }
      case "drag": {
        const from = await this.pointOf(tab, this.nodeOf(tab, args.ref));
        const to = await this.pointOf(tab, this.nodeOf(tab, args.target_ref));
        await this.showTarget(tab, from);
        const steps: [string, Record<string, unknown>][] = [this.mouse("mousePressed", from.x, from.y)];
        for (let step = 1; step <= 5; step += 1) {
          const x = from.x + ((to.x - from.x) * step) / 5;
          const y = from.y + ((to.y - from.y) * step) / 5;
          steps.push(this.mouse("mouseMoved", x, y, { buttons: 1 }));
        }
        steps.push(this.mouse("mouseReleased", to.x, to.y));
        await this.overlay(tab, `move(${to.x}, ${to.y})`);
        dialog = await this.input(tab, steps);
        break;
      }
      case "wait":
        await this.sleep(Math.min(10, Math.max(0, Number(args.seconds ?? 1))) * 1000);
        break;
      case "back":
      case "forward": {
        const history = await this.deps.cdp.send<{ currentIndex: number; entries: { id: number }[] }>(
          tab.tabId,
          "Page.getNavigationHistory",
        );
        const entry = history.entries[history.currentIndex + (action === "back" ? -1 : 1)];
        if (entry === undefined) throw new ToolFailure("failed", `there is no page to go ${action} to`);
        await this.deps.cdp.send(tab.tabId, "Page.navigateToHistoryEntry", { entryId: entry.id });
        tab.world = null;
        settle = true;
        break;
      }
      case "reload":
        await this.deps.cdp.send(tab.tabId, "Page.reload");
        tab.world = null;
        settle = true;
        break;
      case "dialog": {
        if (tab.dialog === null) throw new ToolFailure("failed", "no dialog is open");
        await this.deps.cdp.send(tab.tabId, "Page.handleJavaScriptDialog", {
          accept: args.accept !== false,
          promptText: typeof args.prompt_text === "string" ? args.prompt_text : undefined,
        });
        tab.dialog = null;
        settle = true;
        break;
      }
      default:
        throw new ToolFailure("invalid", "unknown action");
    }
    await this.overlay(tab, "box(null)");
    if (settle && !dialog) {
      await this.sleep(SETTLE_MS);
      if (run.active !== null) await this.waitForLoad(run.active, deadline);
    }
    return this.page(run);
  }

  private async screenshot(run: RunState, fullPage: boolean, deadline: number): Promise<Record<string, unknown>> {
    const tab = await this.prepare(run, deadline);
    const params: Record<string, unknown> = { format: "jpeg", quality: 70 };
    if (fullPage) {
      const metrics = await this.deps.cdp.send<{ cssContentSize: { width: number; height: number } }>(
        tab.tabId,
        "Page.getLayoutMetrics",
      );
      params.captureBeyondViewport = true;
      params.clip = {
        x: 0,
        y: 0,
        width: Math.ceil(metrics.cssContentSize.width),
        height: Math.min(MAX_FULL_PAGE_PX, Math.ceil(metrics.cssContentSize.height)),
        scale: 1,
      };
    }
    await this.overlay(tab, "hidden(true)");
    try {
      let shot = await this.deps.cdp.send<{ data: string }>(tab.tabId, "Page.captureScreenshot", params);
      if (shot.data.length > MAX_SCREENSHOT_CHARS) {
        shot = await this.deps.cdp.send<{ data: string }>(tab.tabId, "Page.captureScreenshot", { ...params, quality: 35 });
      }
      if (shot.data.length > MAX_SCREENSHOT_CHARS) throw new ToolFailure("failed", "the screenshot is too large");
      return { page: await this.page(run), data: shot.data, mime_type: "image/jpeg" };
    } finally {
      await this.overlay(tab, "hidden(false)");
    }
  }

  private async extract(run: RunState, args: Record<string, unknown>, deadline: number): Promise<Record<string, unknown>> {
    const tab = await this.prepare(run, deadline);
    const kind = String(args.kind ?? "text");
    let content: string;
    if (kind === "fields") {
      const fields = (args.fields ?? {}) as Record<string, unknown>;
      const values: Record<string, string> = {};
      for (const [name, ref] of Object.entries(fields)) {
        values[name] = await this.callOn<string>(tab, await this.objectOf(tab, this.nodeOf(tab, ref)), fieldValue);
      }
      content = JSON.stringify(values);
    } else {
      let objectId: string;
      if (typeof args.ref === "string" && args.ref) {
        objectId = await this.objectOf(tab, this.nodeOf(tab, args.ref));
      } else {
        const world = await this.ensureWorld(tab);
        const body = await this.deps.cdp.send<{ result?: { objectId?: string } }>(tab.tabId, "Runtime.evaluate", {
          contextId: world,
          expression: "document.body ?? document.documentElement",
        });
        if (!body.result?.objectId) throw new ToolFailure("failed", "the page has no body");
        objectId = body.result.objectId;
      }
      content = await this.callOn<string>(tab, objectId, extractFrom, [kind]);
    }
    return { page: await this.page(run), kind, content };
  }

  private async tabList(run: RunState): Promise<Record<string, unknown>[]> {
    const list: Record<string, unknown>[] = [];
    for (const [index, tabId] of run.tabs.entries()) {
      const info = await this.deps.tabs.get(tabId);
      list.push({ index, url: info?.url ?? "", title: info?.title ?? "", active: tabId === run.active });
    }
    return list;
  }

  private async tabs(run: RunState, args: Record<string, unknown>, deadline: number): Promise<Record<string, unknown>> {
    const action = String(args.action ?? "list");
    const index = typeof args.index === "number" ? args.index : null;
    if (action === "new") {
      if (run.tabs.length >= MAX_TABS) throw new ToolFailure("failed", `a run can have at most ${MAX_TABS} tabs`);
      const url = typeof args.url === "string" && args.url ? args.url : null;
      if (url !== null) await this.checkSite(run, run.active ?? -1, url, deadline);
      const opener = run.active === null ? null : await this.deps.tabs.get(run.active);
      const created = await this.deps.tabs.create({
        url: url ?? "about:blank",
        windowId: opener?.windowId,
        openerTabId: opener?.id,
      });
      if (!run.tabs.includes(created.id)) run.tabs.push(created.id);
      run.opened.add(created.id);
      run.active = created.id;
      if (url !== null) await this.waitForLoad(created.id, deadline);
    } else if (action === "select") {
      const tabId = index === null ? undefined : run.tabs[index];
      if (tabId === undefined) throw new ToolFailure("failed", "no tab has this index");
      run.active = tabId;
      await this.deps.tabs.update(tabId, { active: true });
    } else if (action === "close") {
      const tabId = index === null ? run.active : run.tabs[index];
      if (tabId === undefined || tabId === null) throw new ToolFailure("failed", "no tab has this index");
      if (!run.opened.has(tabId)) throw new ToolFailure("failed", "sama closes only the tabs that it opened");
      const tab = this.tabStates.get(tabId);
      if (tab?.attached) await this.deps.cdp.detach(tabId).catch(() => undefined);
      await this.deps.tabs.remove(tabId);
      this.onTabRemoved(tabId);
    } else if (action !== "list") {
      throw new ToolFailure("invalid", "unknown tab action");
    }
    return { tabs: await this.tabList(run) };
  }
}
