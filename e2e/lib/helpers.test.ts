import { createHmac } from "node:crypto";
import { deflateRawSync } from "node:zlib";
import { describe, expect, it } from "vitest";
import { DEFAULT_API_URL, DEFAULT_WEB_URL, newRunId, readEnv } from "./env.ts";
import { redact } from "./http.ts";
import { isTestEmail, marker, newPassword, orgName, testEmail } from "./names.ts";
import { signWebhook, webhookHeaders, webhookKey } from "./webhook.ts";
import zip from "./zip.ts";

describe("env", () => {
  it("needs the service key", () => {
    expect(() => readEnv({})).toThrow(/E2E_SUPABASE_SERVICE_ROLE_KEY/);
  });

  it("has staging defaults and trims slashes", () => {
    const env = readEnv({ E2E_SUPABASE_SERVICE_ROLE_KEY: "k", E2E_RUN_ID: "r1" });
    expect(env).toMatchObject({ webUrl: DEFAULT_WEB_URL, apiUrl: DEFAULT_API_URL, runId: "r1" });
    const other = readEnv({
      E2E_SUPABASE_SERVICE_ROLE_KEY: "k",
      E2E_WEB_URL: "http://localhost:3000/",
      E2E_API_URL: "http://localhost:8000//",
    });
    expect(other.webUrl).toBe("http://localhost:3000");
    expect(other.apiUrl).toBe("http://localhost:8000");
  });

  it("makes a run id from the time and a random part", () => {
    expect(newRunId(new Date("2026-11-21T15:30:59Z"), () => 0)).toBe("2611211530-0000");
    expect(newRunId()).toMatch(/^\d{10}-[0-9a-z]{4}$/);
  });
});

describe("names", () => {
  it("marks every test email so the sweep finds it", () => {
    const email = testEmail("2611211530-ab12", "Owner");
    expect(email).toBe("sama-e2e+2611211530-ab12-owner@example.com");
    expect(isTestEmail(email)).toBe(true);
    expect(isTestEmail("someone@example.com")).toBe(false);
    expect(isTestEmail("sama-e2e+x@gmail.com")).toBe(false);
    expect(isTestEmail(null)).toBe(false);
  });

  it("makes org names, markers, and passwords", () => {
    expect(orgName("r1")).toBe("e2e-r1-main");
    expect(marker("chat", () => 0)).toBe("CHAT000000");
    const password = newPassword((n) => new Uint8Array(n).fill(7));
    expect(password.startsWith("E2e-")).toBe(true);
    expect(password.length).toBeGreaterThanOrEqual(20);
  });
});

describe("webhook", () => {
  const secret = `whsec_${Buffer.from("0123456789abcdef0123456789abcdef").toString("base64")}`;

  it("signs as Standard Webhooks does", () => {
    const expected = createHmac("sha256", Buffer.from("0123456789abcdef0123456789abcdef"))
      .update("id1.1700000000.{}")
      .digest("base64");
    expect(signWebhook(secret, "id1", 1700000000, "{}")).toBe(`v1,${expected}`);
    expect(webhookKey(secret).toString()).toBe("0123456789abcdef0123456789abcdef");
  });

  it("sets the three headers with the time in seconds", () => {
    const headers = webhookHeaders(secret, "id2", '{"a":1}', () => 1700000000999);
    expect(headers["webhook-id"]).toBe("id2");
    expect(headers["webhook-timestamp"]).toBe("1700000000");
    expect(headers["webhook-signature"]).toBe(signWebhook(secret, "id2", 1700000000, '{"a":1}'));
  });
});

describe("http", () => {
  it("removes the query from a URL in an error", () => {
    expect(redact("https://a.test/x?token=secret")).toBe("https://a.test/x?…");
    expect(redact("https://a.test/x")).toBe("https://a.test/x");
  });
});

/** A small zip with stored entries, built by hand. */
function makeZip(files: Record<string, string>): Buffer {
  const locals: Buffer[] = [];
  const centrals: Buffer[] = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const data = deflateRawSync(Buffer.from(text));
    const nameBytes = Buffer.from(name);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(8, 8);
    local.writeUInt16LE(nameBytes.length, 26);
    locals.push(local, nameBytes, data);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(8, 10);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBytes);
    offset += local.length + nameBytes.length + data.length;
  }
  const directory = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

describe("zip", () => {
  it("lists the names of the entries", () => {
    const archive = makeZip({ "manifest.json": "{}", "tables/orgs.jsonl": "{}\n" });
    expect(zip.names(archive)).toEqual(["manifest.json", "tables/orgs.jsonl"]);
  });

  it("refuses a file that is not a zip", () => {
    expect(() => zip.names(Buffer.alloc(40))).toThrow(/Not a zip/);
  });
});

describe("sweep", () => {
  it("picks only old users with a test email", async () => {
    const { staleTestUsers } = await import("./sweep.ts");
    const now = Date.parse("2026-11-21T12:00:00Z");
    const users = [
      { id: "1", email: "sama-e2e+a-owner@example.com", created_at: "2026-11-21T06:00:00Z" },
      { id: "2", email: "sama-e2e+b-owner@example.com", created_at: "2026-11-21T11:30:00Z" },
      { id: "3", email: "real.person@example.com", created_at: "2026-01-01T00:00:00Z" },
      { id: "4", email: null, created_at: "2026-01-01T00:00:00Z" },
    ];
    expect(staleTestUsers(users, 3 * 3_600_000, now).map((u) => u.id)).toEqual(["1"]);
  });
});
