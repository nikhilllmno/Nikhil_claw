// The consent of the user for each site (docs/architecture.md, Section 14.2.2). A site is a host
// name. The choice is "allow" (allow on this site) or "never". No choice means "ask". The
// choices live in chrome.storage.local, so they stay after the browser closes. The side panel
// shows them and can remove them.

import type { AsyncStore } from "./auth";
import { hostOf, isBlockedSite } from "./blocklist";

export const SITE_PREFIX = "sama.site.";

export type SiteChoice = "allow" | "never";
export type SiteState = SiteChoice | "ask" | "blocked";

export interface SiteSetting {
  site: string;
  choice: SiteChoice;
}

const keyOf = (site: string) => `${SITE_PREFIX}${site}`;

export class SiteConsent {
  constructor(private readonly store: AsyncStore) {}

  /** The state of the site of `url`. A blocked site is always "blocked". */
  async state(url: string): Promise<SiteState> {
    if (isBlockedSite(url)) return "blocked";
    const site = hostOf(url);
    if (site === null) return "blocked";
    const value = await this.store.get(keyOf(site));
    return value === "allow" || value === "never" ? value : "ask";
  }

  /** Saves a choice for the site of `url`. A blocked site cannot be allowed. */
  async set(url: string, choice: SiteChoice): Promise<void> {
    const site = hostOf(url);
    if (site === null) return;
    if (choice === "allow" && isBlockedSite(url)) return;
    await this.store.set(keyOf(site), choice);
  }

  async remove(site: string): Promise<void> {
    await this.store.remove(keyOf(site));
  }

  async list(): Promise<SiteSetting[]> {
    const keys = (await this.store.keys()).filter((key) => key.startsWith(SITE_PREFIX));
    const settings: SiteSetting[] = [];
    for (const key of keys) {
      const choice = await this.store.get(key);
      if (choice === "allow" || choice === "never") settings.push({ site: key.slice(SITE_PREFIX.length), choice });
    }
    return settings.sort((a, b) => a.site.localeCompare(b.site));
  }
}
