// Sign-up, invites, and roles (docs/architecture.md, Section 17.2): the rules that the screens
// show. The API checks every rule again; these only choose what the screens offer.

import type { InviteRole, OrgMember, OrgRole } from "./api";
import type { KeyValueStore } from "./pkce";

/** localStorage: the token of an invite link, kept while the user signs in or signs up. */
export const PENDING_INVITE_KEY = "sama.invite";
/** A kept invite token ends after this time, so an old one does not stay in the browser. */
export const PENDING_INVITE_TTL_MS = 60 * 60 * 1000;

const TOKEN = /^[A-Za-z0-9_-]{16,200}$/;

/** The link that an admin sends. The token is in the fragment, so it is not in server logs. */
export function inviteLink(origin: string, token: string): string {
  return `${origin.replace(/\/+$/, "")}/invite#${token}`;
}

/** The token of an invite link fragment (`#abc…`), or `null` if it is not a token. */
export function tokenFromFragment(hash: string): string | null {
  const value = hash.startsWith("#") ? hash.slice(1) : hash;
  return TOKEN.test(value) ? value : null;
}

export function keepPendingInvite(store: KeyValueStore, token: string, now: number): void {
  store.setItem(PENDING_INVITE_KEY, JSON.stringify({ token, expires_at: now + PENDING_INVITE_TTL_MS }));
}

/** The kept invite token, or `null`. An old or damaged value is removed. */
export function pendingInvite(store: KeyValueStore, now: number): string | null {
  const raw = store.getItem(PENDING_INVITE_KEY);
  if (raw === null) return null;
  try {
    const saved = JSON.parse(raw) as { token?: unknown; expires_at?: unknown };
    if (
      typeof saved.token === "string" &&
      TOKEN.test(saved.token) &&
      typeof saved.expires_at === "number" &&
      saved.expires_at > now
    ) {
      return saved.token;
    }
  } catch {
    // A damaged value is removed below.
  }
  store.removeItem(PENDING_INVITE_KEY);
  return null;
}

export function forgetPendingInvite(store: KeyValueStore): void {
  store.removeItem(PENDING_INVITE_KEY);
}

export const ROLE_LABELS: Record<OrgRole, string> = {
  owner: "Owner",
  admin: "Admin",
  member: "Member",
};

export function roleBadge(role: OrgRole): string {
  return role === "member" ? "badge" : "badge badge-accent";
}

export function isAdmin(role: OrgRole | null | undefined): boolean {
  return role === "owner" || role === "admin";
}

export function ownerCount(members: readonly OrgMember[]): number {
  return members.filter((m) => m.role === "owner").length;
}

/**
 * The roles that `actor` can give `target`, the current role first. Empty when the actor cannot
 * change the target: a member changes nobody, only an owner changes an owner, and the last
 * owner keeps the role.
 */
export function roleChoices(actor: OrgRole, target: OrgMember, members: readonly OrgMember[]): OrgRole[] {
  if (!isAdmin(actor)) return [];
  if (target.role === "owner" && actor !== "owner") return [];
  if (target.role === "owner" && ownerCount(members) <= 1) return [];
  const all: OrgRole[] = actor === "owner" ? ["owner", "admin", "member"] : ["admin", "member"];
  return [target.role, ...all.filter((r) => r !== target.role)];
}

/** True when `actor` can remove `target`. Removing yourself is leaving. */
export function canRemove(
  actor: OrgRole,
  actorUserId: string,
  target: OrgMember,
  members: readonly OrgMember[],
): boolean {
  if (target.role === "owner" && ownerCount(members) <= 1) return false;
  if (target.user_id === actorUserId) return true;
  if (!isAdmin(actor)) return false;
  return target.role !== "owner" || actor === "owner";
}

/** The roles that an admin can give in an invite. An invite never gives the role owner. */
export const INVITE_ROLES: readonly InviteRole[] = ["member", "admin"];

/** The email of an invite form, or an error text. The API checks it again. */
export function checkInviteEmail(value: string): { email: string } | { error: string } {
  const email = value.trim().toLowerCase();
  if (email === "") return { error: "Give the email of the person." };
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return { error: "This is not an email address." };
  return { email };
}

/** The name of a new org, or an error text. */
export function checkOrgName(value: string): { name: string } | { error: string } {
  const name = value.trim();
  if (name === "") return { error: "Give a name for the org." };
  if (name.length > 100) return { error: "Use 100 characters or fewer." };
  return { name };
}
