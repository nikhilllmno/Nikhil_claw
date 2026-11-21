import { describe, expect, it } from "vitest";
import type { Connection } from "./api";
import {
  serverError,
  canChangeServer,
  canDisableServer,
  canEnableServer,
  enableNotice,
  isMcpServer,
  mcpServers,
  serverAddress,
  serverKindLabel,
  serverTitle,
  testNotice,
} from "./mcpServers";

function connection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: "c1",
    workspace_id: "w1",
    connector: "fetch",
    kind: "mcp_remote",
    scope: "workspace",
    owner_user_id: null,
    state: "connected",
    last_error: null,
    last_health_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    fields: {},
    has_secret: false,
    mcp: null,
    ...overrides,
  };
}

function remote(): NonNullable<Connection["mcp"]> {
  return {
    custom: { title: "Docs", url: "https://mcp.example.com/mcp", auth: "none", header: null },
    include: [],
    exclude: [],
    discovered_at: null,
    tools: [],
  };
}

describe("mcpServers", () => {
  it("keeps remote and hosted MCP servers and drops other kinds", () => {
    const list = [
      connection({ id: "a", kind: "mcp_remote", connector: "b" }),
      connection({ id: "b", kind: "composio", connector: "gmail" }),
      connection({ id: "c", kind: "mcp_hosted", connector: "a" }),
      connection({ id: "d", kind: "http_api", connector: "api" }),
    ];
    expect(mcpServers(list).map((c) => c.id)).toEqual(["c", "a"]);
  });

  it("does not change the list it gets", () => {
    const list = [connection({ id: "a", connector: "z" }), connection({ id: "b", connector: "y" })];
    mcpServers(list);
    expect(list.map((c) => c.id)).toEqual(["a", "b"]);
  });

  it("knows the kinds", () => {
    expect(isMcpServer({ kind: "mcp_hosted" })).toBe(true);
    expect(isMcpServer({ kind: "composio" })).toBe(false);
  });
});

describe("serverTitle, serverKindLabel, serverAddress", () => {
  it("uses the title of the admin, else the name", () => {
    expect(serverTitle(connection({ mcp: remote() }))).toBe("Docs");
    expect(serverTitle(connection())).toBe("fetch");
  });

  it("names the kind", () => {
    expect(serverKindLabel({ kind: "mcp_hosted" })).toBe("Stdio server");
    expect(serverKindLabel({ kind: "mcp_remote" })).toBe("Remote server");
  });

  it("gives the address of a remote server", () => {
    expect(serverAddress(connection({ mcp: remote() }))).toBe(
      "https://mcp.example.com/mcp",
    );
  });

  it("says what a connector host runs", () => {
    const hosted = connection({
      kind: "mcp_hosted",
      mcp: {
        ...remote(),
        custom: {
          title: null,
          package: { registry: "npm", name: "pkg", version: "1.2.3" },
          command: null,
          bin: null,
          args: [],
          env: [],
          egress: [],
          memory_mb: null,
          idle_s: null,
        },
      } as unknown as Connection["mcp"],
    });
    expect(serverAddress(hosted)).toBe("Runs pkg@1.2.3 (npm)");
  });

  it("gives null for a server of the catalog", () => {
    expect(serverAddress(connection())).toBeNull();
  });
});

describe("canChangeServer", () => {
  const member = { admin: false, user_id: "u1" };
  it("lets an admin change any server", () => {
    expect(canChangeServer({ admin: true, user_id: "u9" }, { scope: "workspace", owner_user_id: null })).toBe(true);
  });

  it("uses can_manage of the API when it is there", () => {
    const connection = { scope: "workspace", owner_user_id: null } as const;
    expect(canChangeServer({ admin: true, user_id: "u9" }, { ...connection, can_manage: false })).toBe(false);
    expect(canChangeServer(member, { ...connection, can_manage: true })).toBe(true);
  });

  it("lets a member change only the own personal server", () => {
    expect(canChangeServer(member, { scope: "personal", owner_user_id: "u1" })).toBe(true);
    expect(canChangeServer(member, { scope: "personal", owner_user_id: "u2" })).toBe(false);
    expect(canChangeServer(member, { scope: "workspace", owner_user_id: "u1" })).toBe(false);
  });
});

describe("enable and disable", () => {
  it("offers Enable only for a disabled server", () => {
    expect(canEnableServer({ state: "disabled" })).toBe(true);
    expect(canEnableServer({ state: "connected" })).toBe(false);
    expect(canDisableServer({ state: "disabled" })).toBe(false);
    expect(canDisableServer({ state: "failed" })).toBe(true);
  });
});

describe("testNotice and enableNotice", () => {
  it("reports a good test", () => {
    expect(testNotice({ state: "connected", last_error: null }).tone).toBe("success");
  });

  it("reports a failed test with the error", () => {
    const notice = testNotice({ state: "failed", last_error: "Timeout." });
    expect(notice.tone).toBe("error");
    expect(notice.text).toContain("Timeout.");
  });

  it("reports a server that waits", () => {
    expect(testNotice({ state: "pending", last_error: null }).tone).toBe("info");
  });

  it("reports an enable by the state that the API gives", () => {
    expect(enableNotice({ state: "connected", last_error: null }).tone).toBe("success");
    expect(enableNotice({ state: "failed", last_error: "No answer." }).text).toContain("No answer.");
  });
});

describe("serverError", () => {
  it("gives a sentence for a web page", () => {
    expect(serverError("<!doctype html><html><body>Not found</body></html>")).toContain("web page");
    expect(serverError("&lt;!doctype html&gt;&lt;html&gt;")).toContain("web page");
  });

  it("keeps a short error and cuts a long one", () => {
    expect(serverError("401 Unauthorized")).toBe("401 Unauthorized");
    expect(serverError("x".repeat(400))?.length).toBe(301);
    expect(serverError(null)).toBeNull();
  });
});
