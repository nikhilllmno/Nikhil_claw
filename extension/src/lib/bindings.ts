// The chat of each tab. A tab is bound to one private sama session. The bindings live in
// chrome.storage.session, so they end with the browser, like the tabs. A closed tab loses its
// binding (the session stays in sama and in the web app).

import type { AsyncStore } from "./auth";

export const BINDINGS_KEY = "sama.tabs";

export type Bindings = Record<string, string>;

export class TabBindings {
  constructor(private readonly store: AsyncStore) {}

  private async all(): Promise<Bindings> {
    const value = await this.store.get(BINDINGS_KEY);
    return value !== null && typeof value === "object" ? { ...(value as Bindings) } : {};
  }

  async get(tabId: number): Promise<string | null> {
    return (await this.all())[String(tabId)] ?? null;
  }

  async bind(tabId: number, sessionId: string): Promise<void> {
    const all = await this.all();
    all[String(tabId)] = sessionId;
    await this.store.set(BINDINGS_KEY, all);
  }

  async unbind(tabId: number): Promise<void> {
    const all = await this.all();
    if (!(String(tabId) in all)) return;
    delete all[String(tabId)];
    await this.store.set(BINDINGS_KEY, all);
  }

  /** Removes every binding (sign-out: the next user must not see the chats). */
  async clear(): Promise<void> {
    await this.store.remove(BINDINGS_KEY);
  }
}

/** A chrome.storage area as an `AsyncStore`. */
export function chromeStore(area: chrome.storage.StorageArea): AsyncStore {
  return {
    get: async (key) => (await area.get(key))[key],
    set: (key, value) => area.set({ [key]: value }),
    remove: (key) => area.remove(key),
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
  };
}
