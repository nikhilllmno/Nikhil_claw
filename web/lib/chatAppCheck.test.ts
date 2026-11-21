import { describe, expect, it } from "vitest";
import { activeChatApps, checkNotice } from "./chatAppCheck";

describe("activeChatApps", () => {
  it("lists the active apps once", () => {
    expect(
      activeChatApps([
        { kind: "slack", status: "active", name: "A" },
        { kind: "telegram", status: "failed", name: null },
        { kind: "slack", status: "active", name: "B" },
        { kind: "email", status: "active", name: null },
      ]),
    ).toEqual(["slack"]);
  });

  it("is empty without an active app", () => {
    expect(activeChatApps([])).toEqual([]);
  });
});

describe("checkNotice", () => {
  it("reports a good test with the detail", () => {
    expect(checkNotice("slack", { ok: true, detail: "Team Acme." })).toEqual({
      ok: true,
      text: "The Slack app answers. Team Acme.",
    });
  });

  it("reports a failed test", () => {
    expect(checkNotice("telegram", { ok: false, detail: "Unauthorized." })).toEqual({
      ok: false,
      text: "The Telegram app does not answer. Unauthorized.",
    });
  });

  it("works without a detail", () => {
    expect(checkNotice("slack", { ok: true, detail: null }).text).toBe("The Slack app answers.");
  });
});
