import { describe, expect, it } from "vitest";
import { HELP_TOPICS, helpTopic, searchHelp } from "./help";

describe("help", () => {
  it("has unique topic ids that are safe in a URL", () => {
    const ids = HELP_TOPICS.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(id).toMatch(/^[a-z0-9-]+$/);
  });

  it("links only to pages of the app", () => {
    for (const topic of HELP_TOPICS) {
      for (const link of topic.links) expect(link.href.startsWith("/")).toBe(true);
    }
  });

  it("finds a topic by id and by words", () => {
    expect(helpTopic("data")?.title).toBe("Your data and privacy");
    expect(helpTopic("nope")).toBeNull();
    expect(searchHelp("").length).toBe(HELP_TOPICS.length);
    expect(searchHelp("botfather").map((t) => t.id)).toEqual(["chat-apps"]);
    expect(searchHelp("invite link").map((t) => t.id)).toContain("members");
    expect(searchHelp("zzzz")).toEqual([]);
  });
});
