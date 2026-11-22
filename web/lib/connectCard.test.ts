import { describe, expect, it } from "vitest";
import { bareToolName, cardStatus, cardsOfCalls, parseConnectCards, safeLink } from "./connectCard";

const line = (data: Record<string, unknown>) => `<sama-connect>${JSON.stringify(data)}</sama-connect>`;

describe("connect cards", () => {
  it("reads a card and keeps only https links", () => {
    const text = `The sign-in is open.\n${line({
      connector: "gmail",
      title: "Gmail",
      url: "https://connect.composio.dev/link/1",
      connection_id: "c-1",
      state: "initiated",
      scope: "personal",
    })}`;
    expect(parseConnectCards(text)).toEqual([
      {
        connector: "gmail",
        title: "Gmail",
        url: "https://connect.composio.dev/link/1",
        connectionId: "c-1",
        state: "initiated",
        scope: "personal",
      },
    ]);
    expect(parseConnectCards(line({ connector: "x", title: "X", url: "javascript:alert(1)" }))[0]?.url).toBeNull();
    expect(safeLink("https://user:pw@evil.test/")).toBeNull();
    expect(safeLink("http://plain.test/")).toBeNull();
    expect(parseConnectCards("<sama-connect>{broken}</sama-connect>")).toEqual([]);
    expect(parseConnectCards("no card")).toEqual([]);
  });

  it("takes cards only from finished connect calls", () => {
    const preview = line({ connector: "gmail", title: "Gmail", url: "https://a.test/x" });
    expect(bareToolName("mcp__sama__connection_request")).toBe("connection_request");
    const calls = [
      { name: "mcp__sama__connection_request", status: "done", preview },
      { name: "connection_request", status: "running", preview },
      { name: "web_extract", status: "done", preview },
      { name: "connection_add_mcp", status: "done", preview },
    ];
    expect(cardsOfCalls(calls)).toHaveLength(2);
  });

  it("says what to do for each state", () => {
    expect(cardStatus("connected", "Gmail")).toContain("is connected");
    expect(cardStatus("form", "Notion")).toContain("Never paste");
    expect(cardStatus("initiated", "Gmail")).toContain("Sign in");
    expect(cardStatus("expired", "Gmail")).toContain("again");
  });
});
