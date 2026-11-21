import { describe, expect, it } from "vitest";
import { keyProblem, keyUse, regionText } from "./orgkeys";

describe("keyProblem", () => {
  it("accepts a key of the right form", () => {
    expect(keyProblem("anthropic", ` sk-ant-api03-${"a".repeat(30)} `)).toBeNull();
    expect(keyProblem("openai", `sk-proj-${"b".repeat(30)}`)).toBeNull();
  });

  it("names the problem without the key", () => {
    const key = `pk-${"c".repeat(30)}`;
    expect(keyProblem("openai", "  ")).toBe("Give the OpenAI key.");
    expect(keyProblem("openai", "sk-short")).toBe("The key must have 20 to 512 characters and no spaces.");
    expect(keyProblem("openai", `sk-a b${"c".repeat(30)}`)).toBe(
      "The key must have 20 to 512 characters and no spaces.",
    );
    expect(keyProblem("openai", key)).toBe("An OpenAI key starts with sk-.");
    expect(keyProblem("anthropic", `sk-${"c".repeat(30)}`)).toBe("An Anthropic key starts with sk-ant-.");
    expect(keyProblem("openai", key)).not.toContain(key);
  });
});

describe("keyUse", () => {
  const base = { provider: "openai" as const, updated_at: null };
  it("tells which key the agents use", () => {
    expect(keyUse({ ...base, org_key: true, default_key: true }).badge).toBe("Org key");
    expect(keyUse({ ...base, org_key: false, default_key: true }).badge).toBe("Sama key");
    expect(keyUse({ ...base, org_key: false, default_key: false }).badge).toBe("No key");
  });
});

describe("regionText", () => {
  it("tells if this deployment serves the region", () => {
    expect(regionText({ region: "ap-south-1", region_here: true })).toBe(
      "The org is in region ap-south-1. This deployment serves it.",
    );
    expect(regionText({ region: "eu-central-1", region_here: false })).toBe(
      "The org is in region eu-central-1. Another deployment serves it.",
    );
  });
});
