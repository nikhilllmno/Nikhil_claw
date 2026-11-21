import { describe, expect, it } from "vitest";
import type { OrgMember, OrgRole } from "./api";
import {
  canRemove,
  checkInviteEmail,
  checkOrgName,
  forgetPendingInvite,
  inviteLink,
  inviteToken,
  keepPendingInvite,
  PENDING_INVITE_KEY,
  PENDING_INVITE_TTL_MS,
  pendingInvite,
  replaceMember,
  roleChoices,
  tokenFromFragment,
  toggleWorkspace,
} from "./orgs";
import type { KeyValueStore } from "./pkce";

function store(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

const TOKEN = "Ab_-".repeat(10);

function member(user_id: string, role: OrgRole): OrgMember {
  return { user_id, role, display_name: user_id, email: null, joined_at: null, workspace_ids: [] };
}

describe("invite links", () => {
  it("puts the token in the fragment", () => {
    expect(inviteLink("https://app.test/", TOKEN)).toBe(`https://app.test/invite#${TOKEN}`);
  });

  it("reads only a token from the fragment", () => {
    expect(tokenFromFragment(`#${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromFragment(TOKEN)).toBe(TOKEN);
    expect(tokenFromFragment("")).toBeNull();
    expect(tokenFromFragment("#short")).toBeNull();
    expect(tokenFromFragment("#abc<script>abcdefghijklmnop")).toBeNull();
    expect(tokenFromFragment(`#${"a".repeat(201)}`)).toBeNull();
  });

  it("keeps the token for one hour", () => {
    const s = store();
    keepPendingInvite(s, TOKEN, 1000);
    expect(pendingInvite(s, 1000 + PENDING_INVITE_TTL_MS - 1)).toBe(TOKEN);
    expect(pendingInvite(s, 1000 + PENDING_INVITE_TTL_MS)).toBeNull();
    expect(s.data.has(PENDING_INVITE_KEY)).toBe(false);
  });

  it("removes a damaged value and forgets on request", () => {
    const s = store();
    s.setItem(PENDING_INVITE_KEY, "{not json");
    expect(pendingInvite(s, 0)).toBeNull();
    expect(s.data.has(PENDING_INVITE_KEY)).toBe(false);
    s.setItem(PENDING_INVITE_KEY, JSON.stringify({ token: "bad token", expires_at: 10 }));
    expect(pendingInvite(s, 0)).toBeNull();
    keepPendingInvite(s, TOKEN, 0);
    forgetPendingInvite(s);
    expect(pendingInvite(s, 0)).toBeNull();
  });
});

describe("roles", () => {
  const owner = member("o", "owner");
  const admin = member("a", "admin");
  const plain = member("m", "member");
  const all = [owner, admin, plain];

  it("gives a member no role choices", () => {
    expect(roleChoices("member", plain, all)).toEqual([]);
  });

  it("lets an admin change admins and members, not owners", () => {
    expect(roleChoices("admin", plain, all)).toEqual(["member", "admin"]);
    expect(roleChoices("admin", admin, all)).toEqual(["admin", "member"]);
    expect(roleChoices("admin", owner, all)).toEqual([]);
  });

  it("lets an owner give the role owner, but keeps the last owner", () => {
    expect(roleChoices("owner", plain, all)).toEqual(["member", "owner", "admin"]);
    expect(roleChoices("owner", owner, all)).toEqual([]);
    const two = [...all, member("o2", "owner")];
    expect(roleChoices("owner", owner, two)).toEqual(["owner", "admin", "member"]);
  });

  it("decides who can remove whom", () => {
    expect(canRemove("member", "m", plain, all)).toBe(true);
    expect(canRemove("member", "m", admin, all)).toBe(false);
    expect(canRemove("admin", "a", plain, all)).toBe(true);
    expect(canRemove("admin", "a", owner, all)).toBe(false);
    expect(canRemove("owner", "o", owner, all)).toBe(false);
    const two = [...all, member("o2", "owner")];
    expect(canRemove("owner", "o", two[3] as OrgMember, two)).toBe(true);
    expect(canRemove("admin", "a", two[3] as OrgMember, two)).toBe(false);
  });
});

describe("form checks", () => {
  it("normalizes the invite email", () => {
    expect(checkInviteEmail("  Bob@Example.COM ")).toEqual({ email: "bob@example.com" });
    expect(checkInviteEmail("")).toHaveProperty("error");
    expect(checkInviteEmail("bob@example")).toHaveProperty("error");
  });

  it("trims the org name", () => {
    expect(checkOrgName("  Acme ")).toEqual({ name: "Acme" });
    expect(checkOrgName("   ")).toHaveProperty("error");
    expect(checkOrgName("x".repeat(101))).toHaveProperty("error");
  });
});

describe("review fixes", () => {
  const token = "Cd_-".repeat(10);
  const kept = "Ef_-".repeat(10);

  it("uses the kept token only when the link has no fragment", () => {
    expect(inviteToken(`#${token}`, () => kept)).toBe(token);
    expect(inviteToken("", () => kept)).toBe(kept);
    expect(inviteToken("#", () => kept)).toBe(kept);
    expect(inviteToken("", () => null)).toBeNull();
    // A bad fragment does not fall back to the kept invite.
    expect(inviteToken("#short", () => kept)).toBeNull();
  });

  it("replaces one member and keeps the others", () => {
    const a = member("a", "member");
    const b = member("b", "admin");
    const changed = { ...a, workspace_ids: ["w1"] };
    expect(replaceMember([a, b], changed)).toEqual([changed, b]);
    expect(replaceMember([b], changed)).toEqual([b]);
  });

  it("toggles one workspace", () => {
    expect(toggleWorkspace(["w1"], "w2", true)).toEqual(["w1", "w2"]);
    expect(toggleWorkspace(["w1", "w2"], "w1", false)).toEqual(["w2"]);
    expect(toggleWorkspace(["w1"], "w1", true)).toEqual(["w1"]);
  });
});
