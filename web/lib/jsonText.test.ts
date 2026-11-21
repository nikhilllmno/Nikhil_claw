import { describe, expect, it } from "vitest";
import { parseJsonObject, prettyJson } from "./jsonText";

describe("parseJsonObject", () => {
  it("gives null for an empty field", () => {
    expect(parseJsonObject("", "The payload", 100)).toEqual({ ok: true, value: null });
    expect(parseJsonObject("  \n", "The payload", 100)).toEqual({ ok: true, value: null });
  });

  it("parses an object", () => {
    expect(parseJsonObject('{"a": 1}', "The payload", 100)).toEqual({ ok: true, value: { a: 1 } });
  });

  it("refuses text that is not JSON", () => {
    expect(parseJsonObject("{a:", "The payload", 100)).toEqual({
      ok: false,
      error: "The payload is not valid JSON.",
    });
  });

  it("refuses a value that is not an object", () => {
    for (const text of ["[1]", "3", '"x"', "null", "true"]) {
      const result = parseJsonObject(text, "The config", 100);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error).toContain("The config must be a JSON object");
    }
  });

  it("limits the size in bytes", () => {
    const big = JSON.stringify({ text: "é".repeat(60) });
    const result = parseJsonObject(big, "The payload", 100);
    expect(result).toEqual({ ok: false, error: "The payload has more than 100 bytes." });
    expect(parseJsonObject(big, "The payload", 1000).ok).toBe(true);
  });
});

describe("prettyJson", () => {
  it("writes an indent", () => {
    expect(prettyJson({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
  it("gives an empty text for undefined", () => {
    expect(prettyJson(undefined)).toBe("");
  });
});
