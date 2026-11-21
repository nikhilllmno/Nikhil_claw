// The browser settings of an org on the settings page (docs/architecture.md, Sections 14.1 and
// 16.4). The user name and the password of a saved login go only into the form of the page. The
// API never sends them back.

import type { BrowserLogin, BrowserLoginAdd } from "./api";

export const MAX_NAME_CHARS = 80;
export const MAX_SITE_CHARS = 300;
export const MAX_USERNAME_CHARS = 500;
export const MAX_PASSWORD_CHARS = 1000;
export const MAX_HOSTS = 200;

/** The hosts of a text box: one host for each line, or separated by commas or spaces. */
export function parseHosts(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((host) => host.trim())
    .filter((host) => host !== "");
}

/** The same checks as the API, so that a wrong form fails before it leaves the page. */
export function loginProblem(login: BrowserLoginAdd): string | null {
  const name = login.name.trim();
  if (name === "" || name.length > MAX_NAME_CHARS) return `The name must have 1 to ${MAX_NAME_CHARS} characters.`;
  const site = login.site.trim();
  if (site === "" || site.length > MAX_SITE_CHARS) return "Give the address of the site.";
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(site) && !/^https?:\/\//i.test(site)) {
    return "The site must be an http or https address.";
  }
  if (login.username === "" || login.username.length > MAX_USERNAME_CHARS) return "Give the user name.";
  if (login.username !== login.username.trim()) return "The user name must not start or end with a space.";
  if (login.password === "" || login.password.length > MAX_PASSWORD_CHARS) return "Give the password.";
  return null;
}

export function hostsProblem(allow: string[], block: string[]): string | null {
  if (allow.length > MAX_HOSTS || block.length > MAX_HOSTS) return `A list can have at most ${MAX_HOSTS} hosts.`;
  return null;
}

/** Who can use a login, for its badge. */
export function loginScope(login: BrowserLogin): string {
  if (login.scope === "org") return "Org";
  return login.mine ? "Personal" : "Personal (another member)";
}
