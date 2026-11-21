import { describe, expect, it } from "vitest";

import { chatHome, chatTitle, errorMessage, type Me } from "./api";
import { memoryStore, TabBindings } from "./bindings";
import { normalizeApiUrl } from "./config";
import { parseServerFrame, socketUrl } from "./protocol";

describe("protocol", () => {
  it("builds the socket address", () => {
    expect(socketUrl("https://sama-staging.fly.dev")).toBe("wss://sama-staging.fly.dev/channels/chrome/ws");
    expect(socketUrl("http://localhost:8000/")).toBe("ws://localhost:8000/channels/chrome/ws");
  });

  it("reads only known frames", () => {
    expect(parseServerFrame('{"type":"pong"}')).toEqual({ type: "pong" });
    expect(parseServerFrame('{"type":"tool_call"}')).toBeNull();
    expect(parseServerFrame("nope")).toBeNull();
    expect(parseServerFrame("[]")).toBeNull();
  });
});

describe("config", () => {
  it("accepts https and local http", () => {
    expect(normalizeApiUrl(" https://sama.example.com/ ")).toBe("https://sama.example.com");
    expect(normalizeApiUrl("http://localhost:8000")).toBe("http://localhost:8000");
  });

  it.each(["http://sama.example.com", "ftp://x", "nope", "https://u:p@x.com", "https://x.com/?a=1"])(
    "refuses %s",
    (value) => {
      expect(() => normalizeApiUrl(value)).toThrow();
    },
  );
});

describe("api helpers", () => {
  const me: Me = {
    orgs: [
      {
        id: "o",
        name: "Org",
        user_id: "u",
        display_name: null,
        workspaces: [
          { id: "w0", name: "Empty" },
          { id: "w1", name: "Main" },
        ],
        agent_profiles: [{ id: "p1", name: "Agent", workspace_id: "w1" }],
      },
    ],
  };

  it("chooses the first workspace with an agent", () => {
    expect(chatHome(me)).toEqual({ workspaceId: "w1", agentProfileId: "p1", orgName: "Org" });
    expect(chatHome({ orgs: [] })).toBeNull();
  });

  it("names the chat after the tab", () => {
    expect(chatTitle("  A   page ")).toBe("Chrome: A page");
    expect(chatTitle(undefined)).toBe("Chrome: new tab");
    expect(chatTitle("x".repeat(500))).toHaveLength(120);
  });

  it("reads error bodies", () => {
    expect(errorMessage(422, { detail: [{ msg: "too long" }] })).toBe("too long");
    expect(errorMessage(403, { detail: "share a page only in a private session of your own" })).toContain("private");
    expect(errorMessage(429, null)).toContain("Too many");
  });
});

describe("TabBindings", () => {
  it("binds, unbinds, and clears", async () => {
    const bindings = new TabBindings(memoryStore());
    await bindings.bind(1, "s1");
    await bindings.bind(2, "s2");
    expect(await bindings.get(1)).toBe("s1");
    await bindings.unbind(1);
    expect(await bindings.get(1)).toBeNull();
    await bindings.clear();
    expect(await bindings.get(2)).toBeNull();
  });

  it("keeps concurrent bindings of two tabs and other keys on clear", async () => {
    const store = memoryStore();
    const bindings = new TabBindings(store);
    await store.set("sama.auth", "kept");
    await Promise.all([bindings.bind(1, "s1"), bindings.bind(2, "s2")]);
    expect([await bindings.get(1), await bindings.get(2)]).toEqual(["s1", "s2"]);
    await bindings.clear();
    expect(await store.get("sama.auth")).toBe("kept");
  });
});
