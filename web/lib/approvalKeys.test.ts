import { describe, expect, it } from "vitest";
import { approvalKey, typingInField } from "./approvalKeys";

const key = (k: string, extra: Partial<KeyboardEvent> = {}) => ({
  key: k,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  repeat: false,
  ...extra,
});

describe("approvalKey", () => {
  it("maps 1, 2, and 3", () => {
    expect(approvalKey(key("1"), null, true)).toBe("approve");
    expect(approvalKey(key("2"), null, true)).toBe("always");
    expect(approvalKey(key("3"), null, true)).toBe("deny");
    expect(approvalKey(key("4"), null, true)).toBeNull();
  });

  it("has no always for a proposal", () => {
    expect(approvalKey(key("2"), null, false)).toBeNull();
  });

  it("ignores keys with a modifier or while the person types", () => {
    expect(approvalKey(key("1", { ctrlKey: true }), null, true)).toBeNull();
    expect(approvalKey(key("1", { repeat: true }), null, true)).toBeNull();
    expect(approvalKey(key("1"), { tagName: "INPUT", id: "title" }, true)).toBeNull();
    expect(approvalKey(key("1"), { tagName: "TEXTAREA", id: "message", value: "draft" }, true)).toBeNull();
    expect(approvalKey(key("1"), { tagName: "TEXTAREA", id: "message", value: "" }, true)).toBe("approve");
  });

  it("knows when the focus takes text", () => {
    expect(typingInField({ isContentEditable: true })).toBe(true);
    expect(typingInField({ tagName: "BUTTON" })).toBe(false);
    expect(typingInField(null)).toBe(false);
  });
});
