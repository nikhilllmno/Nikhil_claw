import { describe, expect, it } from "vitest";
import type { BrowserLogin } from "./api";
import { hostsProblem, loginProblem, loginScope, parseHosts } from "./browser";

const good = { name: "GitHub", site: "github.com", username: "me@example.com", password: "pw", scope: "personal" as const };

describe("browser settings", () => {
  it("parses a list of hosts", () => {
    expect(parseHosts(" a.com\nb.com, c.com  d.com\n\n")).toEqual(["a.com", "b.com", "c.com", "d.com"]);
    expect(parseHosts("   ")).toEqual([]);
  });

  it("checks a login before it leaves the page", () => {
    expect(loginProblem(good)).toBeNull();
    expect(loginProblem({ ...good, name: " " })).toMatch(/name/);
    expect(loginProblem({ ...good, site: "ftp://x.com" })).toMatch(/http or https/);
    expect(loginProblem({ ...good, username: " me" })).toMatch(/space/);
    expect(loginProblem({ ...good, password: "" })).toMatch(/password/);
  });

  it("limits the lists", () => {
    expect(hostsProblem([], [])).toBeNull();
    expect(hostsProblem(Array.from({ length: 201 }, (_, n) => `h${n}.com`), [])).toMatch(/200/);
  });

  it("names the scope of a login", () => {
    const login = { scope: "personal", mine: true } as BrowserLogin;
    expect(loginScope(login)).toBe("Personal");
    expect(loginScope({ ...login, mine: false })).toBe("Personal (another member)");
    expect(loginScope({ ...login, scope: "org" })).toBe("Org");
  });
});
