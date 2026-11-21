import { describe, expect, it } from "vitest";

import { Actor, type ActionAnswer, type Prompter, type SiteAnswer, type TabInfo } from "./actions";
import type { AXNode, DomNode } from "./axtree";
import { memoryStore } from "./bindings";
import { SiteConsent } from "./consent";
import type { ToolCallFrame } from "./protocol";
import type { ElementFacts } from "./sensitive";

const DOM: DomNode = {
  nodeType: 9,
  nodeName: "#document",
  backendNodeId: 1,
  children: [
    {
      nodeType: 1,
      nodeName: "FORM",
      backendNodeId: 2,
      attributes: ["method", "post"],
      children: [
        { nodeType: 1, nodeName: "INPUT", backendNodeId: 3, attributes: ["name", "user"] },
        { nodeType: 1, nodeName: "BUTTON", backendNodeId: 4, attributes: [] },
      ],
    },
    { nodeType: 1, nodeName: "BUTTON", backendNodeId: 5, attributes: ["type", "button"] },
  ],
};

const AX: AXNode[] = [
  { nodeId: "1", role: { value: "RootWebArea" }, name: { value: "Shop" }, childIds: ["2", "3", "4"] },
  { nodeId: "2", parentId: "1", role: { value: "textbox" }, name: { value: "User" }, backendDOMNodeId: 3 },
  { nodeId: "3", parentId: "1", role: { value: "button" }, name: { value: "Place order" }, backendDOMNodeId: 4 },
  { nodeId: "4", parentId: "1", role: { value: "button" }, name: { value: "Hello" }, backendDOMNodeId: 5 },
];

const FACTS: Record<string, ElementFacts> = {
  o3: { tag: "input", type: "", role: "", name: "", autocomplete: "", field: "user", form: { method: "post" } },
  o4: { tag: "button", type: "", role: "", name: "Place order", autocomplete: "", field: "", form: { method: "post" } },
  o5: { tag: "button", type: "button", role: "", name: "Hello", autocomplete: "", field: "", form: null },
};

function rig(siteAnswers: SiteAnswer[] = ["allow"], actionAnswers: ActionAnswer[] = []) {
  const sent: string[] = [];
  const cancelled: string[] = [];
  const asked: string[] = [];
  const tab: TabInfo = { id: 1, url: "https://shop.test/", title: "Shop", status: "complete", windowId: 1 };
  const consent = new SiteConsent(memoryStore());
  const prompter: Prompter = {
    site: async (request) => {
      asked.push(`site ${request.site}`);
      return siteAnswers.shift() ?? "deny";
    },
    action: async (request) => {
      asked.push(`action ${request.reason}`);
      return actionAnswers.shift() ?? "deny";
    },
    drop: () => undefined,
  };
  const actor = new Actor({
    cdp: {
      attach: async () => undefined,
      detach: async () => undefined,
      send: async <T,>(_tabId: number, method: string, params: Record<string, unknown> = {}) => {
        sent.push(method);
        const expression = String(params.expression ?? "");
        const answers: Record<string, unknown> = {
          "Page.getFrameTree": { frameTree: { frame: { id: "f" } } },
          "Page.createIsolatedWorld": { executionContextId: 9 },
          "DOM.getDocument": { root: DOM },
          "Accessibility.getFullAXTree": { nodes: AX },
          "DOM.resolveNode": { object: { objectId: `o${String(params.backendNodeId)}` } },
          "DOM.getContentQuads": { quads: [[0, 0, 10, 0, 10, 10, 0, 10]] },
          "Runtime.evaluate": expression.includes("typeof globalThis.__sama") ? { result: { value: true } } : {},
          "Runtime.callFunctionOn": String(params.functionDeclaration).includes("elementFacts")
            ? { result: { value: FACTS[String(params.objectId)] } }
            : { result: { value: null } },
        };
        return (answers[method] ?? {}) as T;
      },
    },
    tabs: {
      get: async (tabId) => (tabId === tab.id ? tab : null),
      create: async () => tab,
      update: async () => undefined,
      remove: async () => undefined,
    },
    consent,
    prompter,
    boundTab: async (sessionId) => (sessionId === "s" ? 1 : null),
    cancelRun: async (runId) => {
      cancelled.push(runId);
    },
    onChange: () => undefined,
    sleep: async () => undefined,
  });
  return { actor, sent, cancelled, asked, consent, tab };
}

let n = 0;
const call = (op: ToolCallFrame["op"], args: Record<string, unknown> = {}, approved = false): ToolCallFrame => ({
  type: "tool_call",
  call_id: `c${++n}`,
  op,
  args,
  session_id: "s",
  run_id: "r",
  approved,
  timeout_ms: 5_000,
});

describe("actor", () => {
  it("asks consent once for a site, then snapshots and clicks", async () => {
    const { actor, sent, asked, consent } = rig(["allow"]);
    const snapshot = await actor.handle(call("snapshot"));
    expect(snapshot.ok).toBe(true);
    if (!snapshot.ok) return;
    expect(String(snapshot.result.tree)).toContain('button "Place order" [ref=e2] [needs approval]');
    expect(snapshot.result.sensitive).toEqual({ e2: "pay" });
    expect(snapshot.result.submits).toEqual(["e1"]);
    expect(await consent.state("https://shop.test/x")).toBe("allow");
    const click = await actor.handle(call("act", { action: "click", ref: "e3" }));
    expect(click.ok).toBe(true);
    expect(asked).toEqual(["site shop.test"]);
    expect(sent.filter((method) => method === "Input.dispatchMouseEvent")).toHaveLength(3);
    expect(actor.agentTabs()).toEqual([{ tabId: 1, runId: "r", sessionId: "s", paused: false }]);
  });

  it("refuses a site that the user does not allow", async () => {
    const { actor } = rig(["deny"]);
    const result = await actor.handle(call("snapshot"));
    expect(result).toMatchObject({ ok: false, error: { code: "consent_denied" } });
  });

  it("refuses blocked sites and pages that are not web pages", async () => {
    const { actor, asked } = rig();
    expect(await actor.handle(call("navigate", { url: "https://accounts.google.com/" }))).toMatchObject({
      ok: false,
      error: { code: "blocked" },
    });
    expect(await actor.handle(call("navigate", { url: "file:///etc/passwd" }))).toMatchObject({
      ok: false,
      error: { code: "not_web" },
    });
    expect(asked).toEqual([]);
  });

  it("asks before a sensitive action that nobody approved", async () => {
    const { actor, asked, sent } = rig(["allow"], ["deny", "allow"]);
    await actor.handle(call("snapshot"));
    const denied = await actor.handle(call("act", { action: "click", ref: "e2" }));
    expect(denied).toMatchObject({ ok: false, error: { code: "sensitive_denied" } });
    expect(sent.filter((method) => method === "Input.dispatchMouseEvent")).toHaveLength(0);
    const allowed = await actor.handle(call("act", { action: "click", ref: "e2" }));
    expect(allowed.ok && allowed.result.notes).toEqual(["approved"]);
    const approved = await actor.handle(call("act", { action: "click", ref: "e2" }, true));
    expect(approved.ok).toBe(true);
    expect(asked).toEqual(["site shop.test", "action buy or pay", "action buy or pay"]);
    const enter = await actor.handle(call("act", { action: "type", ref: "e1", text: "ada", submit: true }));
    expect(enter).toMatchObject({ ok: false, error: { code: "sensitive_denied" } });
  });

  it("fails a stale ref", async () => {
    const { actor } = rig();
    await actor.handle(call("snapshot"));
    expect(await actor.handle(call("act", { action: "click", ref: "e99" }))).toMatchObject({
      ok: false,
      error: { code: "stale_ref" },
    });
  });

  it("pauses when the user takes over and resumes when the user gives back", async () => {
    const { actor } = rig();
    await actor.handle(call("snapshot"));
    actor.onEvent(1, "Runtime.bindingCalled", { name: "__samaSignal", payload: "takeover", executionContextId: 9 });
    await Promise.resolve();
    expect(actor.agentTabs()[0]?.paused).toBe(true);
    const waiting = actor.handle(call("act", { action: "click", ref: "e3" }));
    let done = false;
    void waiting.then(() => {
      done = true;
    });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(done).toBe(false);
    actor.resumeTab(1);
    expect((await waiting).ok).toBe(true);
    // A signal from another world (a page script) does nothing.
    actor.onEvent(1, "Runtime.bindingCalled", { name: "__samaSignal", payload: "takeover", executionContextId: 1 });
    expect(actor.agentTabs()[0]?.paused).toBe(false);
  });

  it("stops: cancels the run, detaches, and refuses later calls", async () => {
    const { actor, cancelled } = rig();
    await actor.handle(call("snapshot"));
    actor.onEvent(1, "Runtime.bindingCalled", { name: "__samaSignal", payload: "stop", executionContextId: 9 });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(cancelled).toEqual(["r"]);
    expect(actor.agentTabs()).toEqual([]);
    expect(await actor.handle(call("snapshot"))).toMatchObject({ ok: false, error: { code: "stopped" } });
    expect((await actor.handle(call("release"))).ok).toBe(true);
  });

  it("stops when the user closes the debugging bar of Chrome", async () => {
    const { actor, cancelled } = rig();
    await actor.handle(call("snapshot"));
    actor.onDetach(1, "canceled_by_user");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(cancelled).toEqual(["r"]);
  });

  it("lists only the tabs of the run and closes only the tabs that it opened", async () => {
    const { actor } = rig();
    const tabs = await actor.handle(call("tabs", { action: "list" }));
    expect(tabs.ok && tabs.result.tabs).toEqual([{ index: 0, url: "https://shop.test/", title: "Shop", active: true }]);
    expect(await actor.handle(call("tabs", { action: "close", index: 0 }))).toMatchObject({
      ok: false,
      error: { code: "failed" },
    });
  });
});
