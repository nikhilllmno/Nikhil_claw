import { describe, expect, it } from "vitest";
import type { Org } from "./api";
import { can, canManageAgents, DEFAULT_SECTION, groupedSections, SECTIONS, sectionById } from "./console";

function org(overrides: Partial<Org> = {}): Org {
  return {
    id: "o1",
    name: "Org",
    slug: "org",
    user_id: "u1",
    display_name: null,
    admin: false,
    role: "member",
    plan: "free",
    workspaces: [],
    agent_profiles: [],
    integrations: [],
    region: "ap-south-1",
    region_here: true,
    region_url: null,
    ...overrides,
  };
}

describe("sections", () => {
  it("has the thirteen sections of the roadmap, each one time", () => {
    const ids = SECTIONS.map((s) => s.id);
    expect(ids).toHaveLength(13);
    expect(new Set(ids).size).toBe(13);
    expect(sectionById(DEFAULT_SECTION)?.title).toBe("Setup");
  });

  it("finds a section by id and refuses an unknown id", () => {
    expect(sectionById("mcp")?.title).toBe("MCP servers");
    expect(sectionById("nope")).toBeNull();
    expect(sectionById(undefined)).toBeNull();
  });

  it("groups the sections in the order of the navigation", () => {
    const groups = groupedSections();
    expect(groups.map((g) => g.group)).toEqual(["Org", "Agents", "Automation", "Activity"]);
    expect(groups.flatMap((g) => g.sections).length).toBe(SECTIONS.length);
  });
});

describe("can", () => {
  it("gives the owner every permission", () => {
    expect(can(org({ role: "owner", admin: true, permissions: [] }), "jobs.create")).toBe(true);
  });

  it("trusts the API when it sends no permissions", () => {
    expect(can(org(), "jobs.create")).toBe(true);
  });

  it("reads the permissions of the role", () => {
    const member = org({ permissions: ["skills.write"] });
    expect(can(member, "skills.write")).toBe(true);
    expect(can(member, "jobs.create")).toBe(false);
  });

  it("needs an admin with agents.manage to manage agents", () => {
    expect(canManageAgents(org({ permissions: ["agents.manage"] }))).toBe(false);
    expect(canManageAgents(org({ role: "admin", admin: true, permissions: ["agents.manage"] }))).toBe(true);
    expect(canManageAgents(org({ role: "admin", admin: true, permissions: [] }))).toBe(false);
  });
});
