import { describe, expect, it } from "vitest";
import { approvalKey, behindModal, escapeTarget, typingInField } from "./approvalKeys";

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
    expect(approvalKey(key("2"), null, true)).toBe("session");
    expect(approvalKey(key("3"), null, true)).toBe("deny");
    expect(approvalKey(key("4"), null, true)).toBeNull();
  });

  it("has no session scope for a proposal", () => {
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

describe("guards", () => {
  const doc = (selectors: string[]) => ({
    querySelector: (s: string) => (selectors.some((x) => s.includes(x)) ? {} : null),
  });

  it("ignores keys that a field or an IME took", () => {
    expect(approvalKey({ ...key("1"), defaultPrevented: true }, null, true)).toBeNull();
    expect(approvalKey({ ...key("1"), isComposing: true }, null, true)).toBeNull();
  });

  it("knows when a modal covers the chat", () => {
    expect(behindModal(null, doc(["dialog[open]"]) )).toBe(true);
    expect(behindModal({ closest: () => ({}) }, doc([]))).toBe(true);
    expect(behindModal({ closest: () => null }, doc([]))).toBe(false);
  });

  it("stops on Esc only from the chat or the message box", () => {
    const none = doc([]) as unknown as Document;
    expect(escapeTarget({ tagName: "BUTTON" }, null, none)).toBe("stop");
    expect(escapeTarget({ tagName: "TEXTAREA", id: "message", value: "x" }, null, none)).toBe("stop");
    expect(escapeTarget({ tagName: "TEXTAREA", id: "plan-edit", value: "x" }, null, none)).toBe("ignore");
    expect(escapeTarget({ tagName: "BUTTON" }, null, doc(["details[data-menu][open]"]) as unknown as Document)).toBe("ignore");
    expect(escapeTarget({ tagName: "BUTTON" }, { closest: () => ({}) }, none)).toBe("ignore");
  });
});
