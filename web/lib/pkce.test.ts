import { describe, expect, it } from "vitest";
import {
  base64url,
  challengeFor,
  type KeyValueStore,
  newPkce,
  PKCE_KEY,
  PKCE_MAX_AGE_MS,
  takeVerifier,
} from "./pkce";

function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

describe("base64url", () => {
  it("uses the URL alphabet and no padding", () => {
    expect(base64url(new Uint8Array([251, 255, 254]))).toBe("-__-");
    expect(base64url(new Uint8Array([1]))).toBe("AQ");
    expect(base64url(new Uint8Array([]))).toBe("");
  });
});

describe("challengeFor", () => {
  it("is the S256 challenge of the RFC 7636 example", async () => {
    expect(await challengeFor("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });
});

describe("newPkce and takeVerifier", () => {
  it("keeps the verifier and returns its challenge", async () => {
    const store = memoryStore();
    const challenge = await newPkce(store, 1000);
    const saved = JSON.parse(store.data.get(PKCE_KEY) ?? "null") as {
      verifier: string;
      created: number;
    };
    expect(saved.created).toBe(1000);
    expect(saved.verifier).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(await challengeFor(saved.verifier)).toBe(challenge);
  });

  it("gives the verifier once", async () => {
    const store = memoryStore();
    await newPkce(store, 1000);
    expect(takeVerifier(store, 2000)).not.toBeNull();
    expect(takeVerifier(store, 2000)).toBeNull();
  });

  it("refuses a verifier older than one hour and removes it", async () => {
    const store = memoryStore();
    await newPkce(store, 0);
    expect(takeVerifier(store, PKCE_MAX_AGE_MS + 1)).toBeNull();
    expect(store.data.has(PKCE_KEY)).toBe(false);
  });

  it("accepts a verifier at exactly one hour", async () => {
    const store = memoryStore();
    await newPkce(store, 0);
    expect(takeVerifier(store, PKCE_MAX_AGE_MS)).not.toBeNull();
  });

  it("refuses a missing or damaged entry", () => {
    const store = memoryStore();
    expect(takeVerifier(store)).toBeNull();
    store.setItem(PKCE_KEY, "{nope");
    expect(takeVerifier(store)).toBeNull();
    store.setItem(PKCE_KEY, JSON.stringify({ verifier: 5, created: 1 }));
    expect(takeVerifier(store)).toBeNull();
  });
});
