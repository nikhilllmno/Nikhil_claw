import { describe, expect, it } from "vitest";
import { tabTarget } from "./focus";

describe("tabTarget", () => {
  it("wraps from the last item to the first and back", () => {
    expect(tabTarget(3, 2, false)).toBe(0);
    expect(tabTarget(3, 0, true)).toBe(2);
  });

  it("lets the browser move inside the panel", () => {
    expect(tabTarget(3, 0, false)).toBeNull();
    expect(tabTarget(3, 1, true)).toBeNull();
  });

  it("brings the focus in when it is outside the panel", () => {
    expect(tabTarget(3, -1, false)).toBe(0);
    expect(tabTarget(3, -1, true)).toBe(2);
  });

  it("does nothing for a panel without focusable items", () => {
    expect(tabTarget(0, -1, false)).toBeNull();
  });

  it("keeps a single item in place", () => {
    expect(tabTarget(1, 0, false)).toBe(0);
    expect(tabTarget(1, 0, true)).toBe(0);
  });
});
