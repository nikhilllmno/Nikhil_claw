import { describe, expect, it } from "vitest";

import { buildTree, domFacts, type AXNode, type DomNode } from "./axtree";
import { memoryStore, TabBindings } from "./bindings";
import { hostOf, isBlockedSite, isWebPage } from "./blocklist";
import { SiteConsent } from "./consent";
import { parseKey } from "./keys";
import { parseServerFrame } from "./protocol";
import { Prompts } from "./prompter";
import { actionReason, clickReason, type ElementFacts, submitsOnEnter, typeReason } from "./sensitive";

const facts = (over: Partial<ElementFacts>): ElementFacts => ({
  tag: "button",
  type: "",
  role: "",
  name: "",
  autocomplete: "",
  field: "",
  form: null,
  ...over,
});

describe("blocklist", () => {
  it("blocks banks, payments, password managers, and Google account pages", () => {
    for (const url of [
      "https://accounts.google.com/",
      "https://www.paypal.com/x",
      "https://vault.bitwarden.com/",
      "https://netbanking.hdfcbank.com/",
      "https://chromewebstore.google.com/detail/a",
      "https://ACCOUNTS.GOOGLE.COM./",
    ]) {
      expect(isBlockedSite(url)).toBe(true);
    }
    for (const url of ["https://example.com/", "https://mail.google.com/", "not a url"]) {
      expect(isBlockedSite(url)).toBe(false);
    }
  });

  it("knows web pages and hosts", () => {
    expect(isWebPage("https://a.test/")).toBe(true);
    expect(isWebPage("chrome://settings")).toBe(false);
    expect(isWebPage(undefined)).toBe(false);
    expect(hostOf("https://Shop.Example.test:8080/x")).toBe("shop.example.test");
    expect(hostOf("nope")).toBeNull();
  });
});

describe("consent", () => {
  it("asks, allows, and refuses for each site", async () => {
    const consent = new SiteConsent(memoryStore());
    expect(await consent.state("https://shop.test/a")).toBe("ask");
    await consent.set("https://shop.test/a", "allow");
    expect(await consent.state("https://shop.test/b")).toBe("allow");
    expect(await consent.state("https://other.test/")).toBe("ask");
    await consent.set("https://other.test/", "never");
    expect(await consent.list()).toEqual([
      { site: "other.test", choice: "never" },
      { site: "shop.test", choice: "allow" },
    ]);
    await consent.remove("shop.test");
    expect(await consent.state("https://shop.test/")).toBe("ask");
  });

  it("never allows a blocked site", async () => {
    const consent = new SiteConsent(memoryStore());
    await consent.set("https://www.paypal.com/", "allow");
    expect(await consent.state("https://www.paypal.com/")).toBe("blocked");
    expect(await consent.list()).toEqual([]);
  });
});

describe("sensitive actions", () => {
  const post = { method: "post" };
  it("finds buttons that pay, send, delete, or submit a form that writes", () => {
    expect(clickReason(facts({ name: "Place order" }))).toBe("pay");
    expect(clickReason(facts({ tag: "a", name: "Delete account" }))).toBe("delete");
    expect(clickReason(facts({ name: "Send" }))).toBe("send");
    expect(clickReason(facts({ form: post, name: "Save" }))).toBe("submit");
    expect(clickReason(facts({ form: { method: "get" }, name: "Search" }))).toBeNull();
    expect(clickReason(facts({ type: "button", form: post, name: "Show more" }))).toBeNull();
    expect(clickReason(facts({ tag: "div", name: "Pay" }))).toBeNull();
    expect(clickReason(facts({ tag: "a", name: "Order history" }))).toBeNull();
  });

  it("finds password and payment fields", () => {
    expect(typeReason(facts({ tag: "input", type: "password" }))).toBe("password");
    expect(typeReason(facts({ tag: "input", autocomplete: "cc-number" }))).toBe("payment");
    expect(typeReason(facts({ tag: "input", field: "cardNumber  " }))).toBe("payment");
    expect(typeReason(facts({ tag: "input", field: "q search" }))).toBeNull();
  });

  it("checks the action on the element", () => {
    const field = facts({ tag: "input", type: "text", form: post });
    expect(submitsOnEnter(field)).toBe(true);
    expect(actionReason({ action: "type", submit: true, key: null }, field)).toBe("submit");
    expect(actionReason({ action: "type", submit: false, key: null }, field)).toBeNull();
    expect(actionReason({ action: "press", submit: false, key: "Enter" }, field)).toBe("submit");
    expect(actionReason({ action: "press", submit: false, key: "Tab" }, field)).toBeNull();
    expect(actionReason({ action: "press", submit: false, key: "Enter" }, facts({ name: "Buy now" }))).toBe("pay");
    expect(actionReason({ action: "hover", submit: false, key: null }, facts({ name: "Pay" }))).toBeNull();
    expect(actionReason({ action: "click", submit: false, key: null }, null)).toBeNull();
  });
});

describe("keys", () => {
  it("parses keys and modifiers", () => {
    expect(parseKey("Enter")).toMatchObject({ key: "Enter", keyCode: 13, text: "\r", modifiers: 0 });
    expect(parseKey("Control+A")).toMatchObject({ key: "a", code: "KeyA", text: "", modifiers: 2 });
    expect(parseKey("Shift+a")).toMatchObject({ key: "A", text: "A", modifiers: 8 });
    expect(parseKey("F5")).toMatchObject({ keyCode: 116 });
    expect(parseKey("Hyper+A")).toBeNull();
    expect(parseKey("NotAKey")).toBeNull();
  });
});

describe("snapshot tree", () => {
  const dom: DomNode = {
    nodeType: 9,
    nodeName: "#document",
    backendNodeId: 1,
    children: [
      {
        nodeType: 1,
        nodeName: "FORM",
        backendNodeId: 2,
        attributes: ["method", "POST", "id", "f"],
        children: [
          { nodeType: 1, nodeName: "INPUT", backendNodeId: 3, attributes: ["type", "password", "name", "pw"] },
          { nodeType: 1, nodeName: "INPUT", backendNodeId: 4, attributes: ["name", "user"] },
          { nodeType: 1, nodeName: "BUTTON", backendNodeId: 5, attributes: [] },
        ],
      },
      { nodeType: 1, nodeName: "BUTTON", backendNodeId: 6, attributes: ["type", "button", "form", "f"] },
    ],
  };
  const ax: AXNode[] = [
    { nodeId: "1", role: { value: "RootWebArea" }, name: { value: "Shop" }, childIds: ["2", "3", "4", "5", "6", "7"] },
    { nodeId: "2", parentId: "1", role: { value: "textbox" }, name: { value: "Password" }, value: { value: "hunter2" }, backendDOMNodeId: 3 },
    { nodeId: "3", parentId: "1", role: { value: "textbox" }, name: { value: "User" }, value: { value: "ada" }, backendDOMNodeId: 4 },
    { nodeId: "4", parentId: "1", role: { value: "button" }, name: { value: "Continue" }, backendDOMNodeId: 5 },
    { nodeId: "5", parentId: "1", role: { value: "generic" }, childIds: ["6"] },
    { nodeId: "6", parentId: "5", role: { value: "StaticText" }, name: { value: "Hello   world" } },
    { nodeId: "7", parentId: "1", role: { value: "button" }, name: { value: "Help" }, ignored: false, properties: [{ name: "disabled", value: { value: true } }], backendDOMNodeId: 6 },
  ];

  it("gives refs, hides passwords, and marks sensitive elements", () => {
    const known = domFacts(dom);
    expect(known.get(6)?.form).toEqual({ method: "post" });
    const result = buildTree(ax, (id) => known.get(id) ?? null);
    expect(result.tree).toBe(
      [
        '- document "Shop"',
        '  - textbox "Password" [ref=e1] [needs approval]',
        '  - textbox "User" [ref=e2]: "ada"',
        '  - button "Continue" [ref=e3] [needs approval]',
        '  - text: "Hello world"',
        '  - button "Help" [disabled] [ref=e4]',
      ].join("\n"),
    );
    expect(result.tree).not.toContain("hunter2");
    expect(result.sensitive).toEqual({ e1: "password", e3: "submit" });
    expect(result.submits).toEqual(["e1", "e2"]);
    expect(result.refs.get("e3")).toBe(5);
  });

  it("cuts a long tree", () => {
    const result = buildTree(ax, () => null, 60);
    expect(result.tree).toContain("the snapshot ends here");
  });
});

describe("prompts", () => {
  it("answers, times out, and drops the questions of a run", async () => {
    const published: number[] = [];
    let opened = 0;
    let id = 0;
    const prompts = new Prompts({
      publish: (list) => published.push(list.length),
      hasViewer: () => false,
      openWindow: () => {
        opened += 1;
      },
      newId: () => `p${++id}`,
    });
    const site = prompts.site({ runId: "r", tabId: 1, site: "a.test" }, 5_000);
    expect(opened).toBe(1);
    expect(prompts.list()[0]).toMatchObject({ kind: "site", site: "a.test" });
    expect(prompts.answer("p1", "once")).toBe(true);
    expect(await site).toBe("once");
    const action = prompts.action({ runId: "r", tabId: 1, site: "a.test", reason: "buy or pay", label: " Pay\n now " }, 5_000);
    expect(prompts.list()[0]?.label).toBe("Pay now");
    expect(prompts.answer("p2", "never")).toBe(false);
    prompts.drop("r");
    expect(await action).toBe("deny");
    expect(await prompts.site({ runId: "r", tabId: 1, site: "a.test" }, 10)).toBe("timeout");
    expect(await prompts.site({ runId: "r", tabId: 1, site: "a.test" }, 0)).toBe("timeout");
    expect(published.at(-1)).toBe(0);
  });
});

describe("protocol and bindings", () => {
  it("reads a tool call frame", () => {
    const frame = parseServerFrame('{"type":"tool_call","call_id":"c","op":"snapshot","args":{}}');
    expect(frame?.type).toBe("tool_call");
  });

  it("finds the tab of a session", async () => {
    const bindings = new TabBindings(memoryStore());
    await bindings.bind(7, "s-1");
    expect(await bindings.tabOf("s-1")).toBe(7);
    expect(await bindings.tabOf("s-2")).toBeNull();
  });
});
