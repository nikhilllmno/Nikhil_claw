// The chat of each tab. A tab is bound to one private sama session. The bindings live in
// chrome.storage.session, so they end with the browser, like the tabs. A closed tab loses its
// binding (the session stays in sama and in the web app).

import type { AsyncStore } from "./auth";

/** The prefix of the storage key of each tab: `sama.tab.<tab id>`. */
export const BINDING_PREFIX = "sama.tab.";

const keyOf = (tabId: number) => `${BINDING_PREFIX}${tabId}`;

/**
 * Each tab has its own storage key, so two writers (two panels, or a panel and the worker) do
 * not overwrite the bindings of other tabs.
 */
export class TabBindings {
  constructor(private readonly store: AsyncStore) {}

  async get(tabId: number): Promise<string | null> {
    const value = await this.store.get(keyOf(tabId));
    return typeof value === "string" ? value : null;
  }

  async bind(tabId: number, sessionId: string): Promise<void> {
    await this.store.set(keyOf(tabId), sessionId);
  }

  /** The tab that is bound to a session, or `null`. */
  async tabOf(sessionId: string): Promise<number | null> {
    for (const key of await this.store.keys()) {
      if (!key.startsWith(BINDING_PREFIX)) continue;
      if ((await this.store.get(key)) === sessionId) {
        const tabId = Number(key.slice(BINDING_PREFIX.length));
        if (Number.isInteger(tabId)) return tabId;
      }
    }
    return null;
  }

  async unbind(tabId: number): Promise<void> {
    await this.store.remove(keyOf(tabId));
  }

  /** Removes every binding (sign-out: the next user must not see the chats). */
  async clear(): Promise<void> {
    const keys = (await this.store.keys()).filter((key) => key.startsWith(BINDING_PREFIX));
    await Promise.all(keys.map((key) => this.store.remove(key)));
  }
}

/** A chrome.storage area as an `AsyncStore`. */
export function chromeStore(area: chrome.storage.StorageArea): AsyncStore {
  return {
    get: async (key) => (await area.get(key))[key],
    set: (key, value) => area.set({ [key]: value }),
    remove: (key) => area.remove(key),
    keys: async () => Object.keys(await area.get(null)),
  };
}

/** A map as an `AsyncStore`, for tests. */
export function memoryStore(): AsyncStore & { data: Map<string, unknown> } {
  const data = new Map<string, unknown>();
  return {
    data,
    get: async (key) => structuredClone(data.get(key)),
    set: async (key, value) => {
      data.set(key, structuredClone(value));
    },
    remove: async (key) => {
      data.delete(key);
    },
    keys: async () => [...data.keys()],
  };
}
