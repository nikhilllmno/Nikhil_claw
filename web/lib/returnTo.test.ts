import { describe, expect, it } from "vitest";
import { RETURN_MAX_AGE_MS, safeReturnPath, saveReturnTo, takeReturnTo } from "./returnTo";

function store() {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
    data,
  };
}

describe("return after sign-in", () => {
  it("keeps the deep link once", () => {
    const s = store();
    saveReturnTo(s, "/connections?workspace=w&connect=gmail&scope=personal", 1000);
    expect(takeReturnTo(s, 2000)).toBe("/connections?workspace=w&connect=gmail&scope=personal");
    expect(takeReturnTo(s, 2000)).toBeNull();
  });

  it("refuses other sites, the login page, and old links", () => {
    expect(safeReturnPath("//evil.test/x")).toBeNull();
    expect(safeReturnPath("/\\evil.test")).toBeNull();
    expect(safeReturnPath("https://evil.test/")).toBeNull();
    expect(safeReturnPath("/login?x=1")).toBeNull();
    expect(safeReturnPath("/")).toBeNull();
    expect(safeReturnPath("/s/abc")).toBe("/s/abc");
    const s = store();
    saveReturnTo(s, "/s/abc", 0);
    expect(takeReturnTo(s, RETURN_MAX_AGE_MS + 1)).toBeNull();
    s.setItem("sama.returnTo", "{not json");
    expect(takeReturnTo(s, 0)).toBeNull();
  });
});
