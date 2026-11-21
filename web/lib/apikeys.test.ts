import { describe, expect, it } from "vitest";
import { approvalsText, clientSnippets, draftBody, emptyDraft, profilesFor, statusBadge } from "./apikeys";

const org = {
  workspaces: [
    { id: "w1", name: "One" },
    { id: "w2", name: "Two" },
  ],
  agent_profiles: [
    {
      id: "p0",
      name: "Org agent",
      workspace_id: null,
      runtime: "claude_agent_sdk",
      model: "m",
    },
    {
      id: "p1",
      name: "One agent",
      workspace_id: "w1",
      runtime: "claude_agent_sdk",
      model: "m",
    },
    {
      id: "p2",
      name: "Two agent",
      workspace_id: "w2",
      runtime: "claude_agent_sdk",
      model: "m",
    },
  ],
};

describe("draftBody", () => {
  it("makes the request body of a good draft", () => {
    const draft = {
      ...emptyDraft(org),
      name: "  CI  ",
      profileId: "p1",
      expiresInDays: " 30 ",
    };
    expect(draftBody(draft)).toEqual({
      body: {
        name: "CI",
        workspace_id: "w1",
        agent_profile_id: "p1",
        scopes: ["chat", "mcp"],
        approvals: "deny",
        expires_in_days: 30,
      },
    });
  });

  it("names the problem of a bad draft", () => {
    const base = { ...emptyDraft(org), name: "x" };
    expect(draftBody({ ...base, name: " " })).toEqual({
      problem: "Give a name of 1 to 80 characters.",
    });
    expect(draftBody({ ...base, chat: false, mcp: false })).toEqual({
      problem: "Choose at least one use for the key.",
    });
    expect(draftBody({ ...base, expiresInDays: "0" })).toEqual({
      problem: "A key expires after 1 to 365 days.",
    });
    expect(draftBody({ ...base, expiresInDays: "1.5" })).toHaveProperty("problem");
    expect(draftBody({ ...base, workspaceId: "" })).toEqual({
      problem: "Choose a workspace.",
    });
  });

  it("keeps an empty agent and expiry as null", () => {
    const result = draftBody({
      ...emptyDraft(org),
      name: "x",
      mcp: false,
      approvals: "pending",
    });
    expect(result).toEqual({
      body: {
        name: "x",
        workspace_id: "w1",
        agent_profile_id: null,
        scopes: ["chat"],
        approvals: "pending",
        expires_in_days: null,
      },
    });
  });
});

describe("profilesFor", () => {
  it("gives the org agents and the agents of the workspace", () => {
    expect(profilesFor(org, "w2").map((p) => p.id)).toEqual(["p0", "p2"]);
  });
});

describe("texts", () => {
  it("shows the status and the approvals", () => {
    expect(statusBadge({ status: "active" }).label).toBe("Active");
    expect(statusBadge({ status: "revoked" }).label).toBe("Revoked");
    expect(statusBadge({ status: "expired" }).label).toBe("Expired");
    expect(approvalsText("deny")).toContain("denied");
    expect(approvalsText("pending")).toContain("waits");
  });

  it("puts the API address in the client examples", () => {
    const snippets = clientSnippets("https://sama.example/");
    expect(snippets.openai).toContain('base_url="https://sama.example/v1"');
    expect(snippets.mcp).toContain("https://sama.example/mcp");
    expect(snippets.openai).not.toContain("sama_sk_");
  });
});
