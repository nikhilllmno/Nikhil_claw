import { describe, expect, it } from "vitest";
import type { Job } from "./api";
import {
  approvalNotice,
  approvalReasonText,
  availableActions,
  browserTimeZone,
  buildInput,
  deliveryStatusLabel,
  deliveryStatusTone,
  failureNotice,
  fireStatusLabel,
  fireStatusTone,
  formatTarget,
  formatWhen,
  newDraft,
  parsePreapprovedTools,
  parseSkills,
  parseTarget,
  scheduleToText,
  sortJobs,
  statusLabel,
  statusTone,
  toDraft,
  validateDraft,
} from "./jobs";

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    name: "Digest",
    status: "active",
    version: 2,
    approved_version: 2,
    needs_approval: false,
    approval_reasons: [],
    schedule: { kind: "cron", expr: "0 9 * * 1-5", tz: "Asia/Kolkata" },
    schedule_text: "cron 0 9 * * 1-5 (Asia/Kolkata)",
    next_fire_at: "2026-11-23T03:30:00+00:00",
    next_fires: [],
    last_fire_at: null,
    prompt: "Send the digest.",
    skills: [],
    model: null,
    mode: "agent",
    script: null,
    deliver: "origin",
    failure_deliver: null,
    preapproved_tools: [],
    attach_to_session: false,
    workspace_id: "w1",
    agent_profile_id: "p1",
    consecutive_failures: 0,
    paused_reason: null,
    created_by_kind: "user",
    created_at: "2026-11-20T00:00:00+00:00",
    updated_at: "2026-11-20T00:00:00+00:00",
    fires: [],
    ...overrides,
  };
}

describe("labels and tones", () => {
  it("names each status of a job", () => {
    expect(statusLabel("active")).toBe("Active");
    expect(statusLabel("paused")).toBe("Paused");
    expect(statusLabel("pending_approval")).toBe("Needs approval");
    expect(statusLabel("removed")).toBe("Removed");
    expect(statusLabel("other")).toBe("other");
    expect(statusTone("active")).toBe("accent");
    expect(statusTone("pending_approval")).toBe("warning");
    expect(statusTone("paused")).toBe("neutral");
  });

  it("names fires and deliveries", () => {
    expect(fireStatusLabel("running")).toBe("Running");
    expect(fireStatusLabel("completed")).toBe("Completed");
    expect(fireStatusLabel("failed")).toBe("Failed");
    expect(fireStatusLabel("skipped")).toBe("Skipped");
    expect(fireStatusLabel("new")).toBe("new");
    expect(fireStatusTone("completed")).toBe("accent");
    expect(fireStatusTone("failed")).toBe("danger");
    expect(fireStatusTone("skipped")).toBe("neutral");
    expect(deliveryStatusLabel("sent")).toBe("Sent");
    expect(deliveryStatusLabel("failed")).toBe("Not sent");
    expect(deliveryStatusLabel("unknown")).toBe("Not known if sent");
    expect(deliveryStatusLabel("pending")).toBe("Waiting to send");
    expect(deliveryStatusLabel("sending")).toBe("Sending");
    expect(deliveryStatusTone("sent")).toBe("accent");
    expect(deliveryStatusTone("failed")).toBe("danger");
    expect(deliveryStatusTone("unknown")).toBe("warning");
    expect(deliveryStatusTone("pending")).toBe("neutral");
  });

  it("writes the reasons for an approval as sentences", () => {
    expect(approvalReasonText("it sends to slack:#ops")).toBe("It sends to slack:#ops.");
    expect(approvalReasonText("it runs a script in the sandbox.")).toBe("It runs a script in the sandbox.");
    expect(approvalReasonText("  ")).toBe("");
    const notice = approvalNotice(
      job({
        version: 3,
        created_by_kind: "agent",
        approval_reasons: ["it pre-approves tools: web_fetch", ""],
      }),
    );
    expect(notice.summary).toBe("An agent made version 3 of this job. It does not run until you approve it.");
    expect(notice.reasons).toEqual(["It pre-approves tools: web_fetch."]);
    expect(approvalNotice(job({ created_by_kind: "user" })).summary).toMatch(/^Someone made version 2/);
  });

  it("says how many fires failed in a row", () => {
    expect(failureNotice(0)).toBe("");
    expect(failureNotice(1)).toBe("The last fire failed.");
    expect(failureNotice(3)).toBe("The last 3 fires failed.");
  });

  it("offers the actions that fit the status", () => {
    expect(availableActions("active")).toEqual(["run", "pause", "cancel", "remove"]);
    expect(availableActions("paused")).toEqual(["run", "resume", "cancel", "remove"]);
    expect(availableActions("pending_approval")).toEqual(["cancel", "remove"]);
    expect(availableActions("removed")).toEqual([]);
  });
});

describe("sortJobs", () => {
  it("puts pending jobs first, then active by next fire, then paused", () => {
    const sorted = sortJobs([
      job({ id: "paused", name: "p", status: "paused", next_fire_at: null }),
      job({ id: "late", name: "l", next_fire_at: "2026-12-01T00:00:00Z" }),
      job({ id: "removed", name: "r", status: "removed", next_fire_at: null }),
      job({ id: "soon", name: "s", next_fire_at: "2026-11-22T00:00:00Z" }),
      job({ id: "pending", name: "w", status: "pending_approval", next_fire_at: null }),
    ]);
    expect(sorted.map((j) => j.id)).toEqual(["pending", "soon", "late", "paused", "removed"]);
  });

  it("sorts by name when the next fire is the same, and does not change the input", () => {
    const input = [job({ id: "b", name: "b" }), job({ id: "a", name: "a" })];
    expect(sortJobs(input).map((j) => j.id)).toEqual(["a", "b"]);
    expect(input.map((j) => j.id)).toEqual(["b", "a"]);
  });
});

describe("times", () => {
  it("formats a time in the zone of the job", () => {
    const text = formatWhen("2026-11-23T03:30:00+00:00", "Asia/Kolkata", "en-US");
    expect(text).toContain("9:00");
    expect(text).toContain("Nov 23, 2026");
  });

  it("falls back to the browser zone for a bad zone, and is empty for a bad time", () => {
    expect(formatWhen("2026-11-23T03:30:00Z", "Not/AZone", "en-US")).toContain("2026");
    expect(formatWhen("2026-11-23T03:30:00Z", null, "en-US")).toContain("2026");
    expect(formatWhen("not a time", "UTC")).toBe("");
    expect(formatWhen(null)).toBe("");
    expect(formatWhen("")).toBe("");
  });

  it("finds a time zone for the browser", () => {
    expect(browserTimeZone()).not.toBe("");
  });

  it("writes a schedule as text that the API can read", () => {
    expect(scheduleToText({ kind: "cron", expr: "0 9 * * 1-5", tz: "UTC" }, "x")).toBe("0 9 * * 1-5");
    expect(scheduleToText({ kind: "interval", every_s: 7200 }, "x")).toBe("every 2 hours");
    expect(scheduleToText({ kind: "interval", every_s: 3600 }, "x")).toBe("every 1 hour");
    expect(scheduleToText({ kind: "interval", every_s: 86400 }, "x")).toBe("every 1 day");
    expect(scheduleToText({ kind: "interval", every_s: 90 }, "x")).toBe("every 90 seconds");
    expect(scheduleToText({ kind: "interval", every_s: 120 }, "x")).toBe("every 2 minutes");
    expect(scheduleToText({ kind: "once", at: "2026-12-01T09:00:00+05:30" }, "once at 2026-12-01")).toBe(
      "once at 2026-12-01",
    );
  });
});

describe("parseTarget", () => {
  it("accepts origin and none", () => {
    expect(parseTarget(" origin ")).toEqual({ ok: true, value: "origin" });
    expect(parseTarget("none")).toEqual({ ok: true, value: "none" });
  });

  it("accepts a Slack channel with or without the hash", () => {
    expect(parseTarget("slack:#ops")).toEqual({ ok: true, value: "slack:#ops" });
    expect(parseTarget("Slack: general")).toEqual({ ok: true, value: "slack:general" });
    expect(parseTarget("slack:#a b").ok).toBe(false);
    expect(parseTarget("slack:").ok).toBe(false);
    expect(parseTarget(`slack:${"a".repeat(81)}`).ok).toBe(false);
  });

  it("accepts a Telegram chat id, also a negative one", () => {
    expect(parseTarget("telegram:12345")).toEqual({ ok: true, value: "telegram:12345" });
    expect(parseTarget("telegram:-100123")).toEqual({ ok: true, value: "telegram:-100123" });
    expect(parseTarget("telegram:abc").ok).toBe(false);
    expect(parseTarget(`telegram:${"1".repeat(21)}`).ok).toBe(false);
  });

  it("accepts an email address and makes it lower case", () => {
    expect(parseTarget("email:Me@Example.com")).toEqual({ ok: true, value: "email:me@example.com" });
    expect(parseTarget("email:me@example").ok).toBe(false);
    expect(parseTarget("email:me example@x.com").ok).toBe(false);
  });

  it("names the field in the error", () => {
    const result = parseTarget("sms:123", "failure_deliver");
    expect(result).toEqual({
      ok: false,
      error: "failure_deliver must be origin, none, slack:<channel>, telegram:<chat id>, or email:<address>.",
    });
    expect(parseTarget("").ok).toBe(false);
    expect(parseTarget("origin:x").ok).toBe(false);
  });

  it("writes a target as words", () => {
    expect(formatTarget("origin")).toBe("The chat of the job");
    expect(formatTarget("none")).toBe("No delivery");
    expect(formatTarget("slack:#ops")).toBe("Slack #ops");
    expect(formatTarget("slack:ops")).toBe("Slack #ops");
    expect(formatTarget("telegram:42")).toBe("Telegram chat 42");
    expect(formatTarget("email:a@b.co")).toBe("Email a@b.co");
    expect(formatTarget("weird")).toBe("weird");
  });
});

describe("parsePreapprovedTools", () => {
  it("gives an empty list for empty text", () => {
    expect(parsePreapprovedTools("  ")).toEqual({ ok: true, value: [] });
  });

  it("reads tools with limits and a connection", () => {
    const result = parsePreapprovedTools(
      JSON.stringify([
        { tool: " web_fetch ", limits: { host: ["example.com"], n: [1, true] } },
        { tool: "x", connection: "c" },
      ]),
    );
    expect(result).toEqual({
      ok: true,
      value: [
        { tool: "web_fetch", limits: { host: ["example.com"], n: [1, true] } },
        { tool: "x", limits: {}, connection: "c" },
      ],
    });
  });

  it("explains each kind of mistake", () => {
    const error = (text: string) => {
      const result = parsePreapprovedTools(text);
      return result.ok ? null : result.error;
    };
    expect(error("{")).toBe("Pre-approved tools must be valid JSON.");
    expect(error("{}")).toBe("Pre-approved tools must be a list.");
    expect(error("[1]")).toBe("Pre-approved tool 1 must be an object.");
    expect(error('[{"tool":""}]')).toBe("Pre-approved tool 1 needs a tool name.");
    expect(error('[{"tool":"a","extra":1}]')).toBe("Pre-approved tool 1 has an unknown field: extra.");
    expect(error('[{"tool":"a","connection":3}]')).toBe("Pre-approved tool 1: connection must be text.");
    expect(error('[{"tool":"a","limits":[]}]')).toBe("Pre-approved tool 1: limits must be an object.");
    expect(error('[{"tool":"a","limits":{"k":[]}}]')).toBe(
      "Pre-approved tool 1: the limit k must be a list of values.",
    );
    expect(error('[{"tool":"a","limits":{"k":[{}]}}]')).toBe(
      "Pre-approved tool 1: the limit k must be a list of values.",
    );
    expect(error(JSON.stringify(Array.from({ length: 21 }, () => ({ tool: "a" }))))).toBe(
      "A job can pre-approve at most 20 tools.",
    );
  });
});

describe("parseSkills", () => {
  it("splits on commas and spaces and drops repeats", () => {
    expect(parseSkills("a, b  c,,a")).toEqual(["a", "b", "c"]);
    expect(parseSkills("")).toEqual([]);
  });
});

describe("validateDraft", () => {
  const valid = () => ({ ...newDraft("UTC"), schedule: "daily at 9am", prompt: "Do it." });

  it("accepts a valid agent draft and a valid script draft", () => {
    expect(validateDraft(valid())).toBeNull();
    expect(validateDraft({ ...valid(), mode: "script", prompt: "", script: "echo hi" })).toBeNull();
  });

  it("asks for the required fields", () => {
    expect(validateDraft({ ...valid(), schedule: " " })).toBe("Write when the job runs.");
    expect(validateDraft({ ...valid(), tz: "" })).toBe("Write a time zone, for example UTC.");
    expect(validateDraft({ ...valid(), prompt: "" })).toBe("Write a prompt for the agent.");
    expect(validateDraft({ ...valid(), mode: "script", script: " " })).toBe("Write a script.");
  });

  it("checks the sizes", () => {
    expect(validateDraft({ ...valid(), name: "n".repeat(201) })).toMatch(/name has more than 200/);
    expect(validateDraft({ ...valid(), tz: "z".repeat(65) })).toBe("The time zone is too long.");
    expect(validateDraft({ ...valid(), prompt: "p".repeat(20_001) })).toMatch(/prompt has more than/);
    expect(validateDraft({ ...valid(), mode: "script", script: "s".repeat(20_001) })).toMatch(
      /script has more than/,
    );
  });

  it("checks the targets, but an empty one is fine", () => {
    expect(validateDraft({ ...valid(), deliver: "" })).toBeNull();
    expect(validateDraft({ ...valid(), failureDeliver: "" })).toBeNull();
    expect(validateDraft({ ...valid(), deliver: "sms:1" })).toMatch(/^deliver must be/);
    expect(validateDraft({ ...valid(), failureDeliver: "sms:1" })).toMatch(/^failure_deliver must be/);
    expect(validateDraft({ ...valid(), deliver: `slack:${"a".repeat(300)}` })).toBe("deliver is too long.");
  });

  it("checks the model, the skills, and the tools", () => {
    expect(validateDraft({ ...valid(), model: "claude sonnet" })).toBe("The model name is not valid.");
    expect(validateDraft({ ...valid(), model: "claude-sonnet-5-5" })).toBeNull();
    expect(validateDraft({ ...valid(), skills: "Bad_Name" })).toBe("The skill name Bad_Name is not valid.");
    expect(
      validateDraft({ ...valid(), skills: Array.from({ length: 21 }, (_, i) => `s${i}`).join(",") }),
    ).toBe("A job can use at most 20 skills.");
    expect(validateDraft({ ...valid(), preapprovedTools: "nope" })).toBe(
      "Pre-approved tools must be valid JSON.",
    );
    expect(
      validateDraft({ ...valid(), mode: "script", script: "x", preapprovedTools: '[{"tool":"a"}]' }),
    ).toBe("A script job has no tools to pre-approve.");
  });
});

describe("toDraft", () => {
  it("copies a job into the form", () => {
    const draft = toDraft(
      job({
        failure_deliver: "email:a@b.co",
        model: "m",
        skills: ["a", "b"],
        attach_to_session: true,
        preapproved_tools: [{ tool: "web_fetch", connection: null, limits: {} }],
      }),
    );
    expect(draft).toMatchObject({
      name: "Digest",
      schedule: "0 9 * * 1-5",
      tz: "Asia/Kolkata",
      prompt: "Send the digest.",
      deliver: "origin",
      failureDeliver: "email:a@b.co",
      model: "m",
      attachToSession: true,
      mode: "agent",
      skills: "a, b",
      agentProfileId: "p1",
    });
    expect(JSON.parse(draft.preapprovedTools)).toEqual([{ tool: "web_fetch", connection: null, limits: {} }]);
  });

  it("uses UTC for a schedule without a zone and the script mode", () => {
    const draft = toDraft(
      job({ schedule: { kind: "interval", every_s: 3600 }, mode: "script", script: "echo" }),
    );
    expect(draft.tz).toBe("UTC");
    expect(draft.mode).toBe("script");
    expect(draft.script).toBe("echo");
    expect(draft.preapprovedTools).toBe("");
  });
});

describe("buildInput for a new job", () => {
  it("sends the fields that have a value", () => {
    const draft = {
      ...newDraft("Asia/Kolkata", "p1"),
      name: " Digest ",
      schedule: " every weekday at 9am ",
      prompt: " Send it. ",
    };
    expect(buildInput(draft, null, { workspaceId: "w1" })).toEqual({
      name: "Digest",
      schedule: "every weekday at 9am",
      tz: "Asia/Kolkata",
      mode: "agent",
      prompt: "Send it.",
      deliver: "origin",
      agent_profile_id: "p1",
      workspace_id: "w1",
    });
  });

  it("leaves out empty optional fields", () => {
    const input = buildInput({ ...newDraft("UTC"), schedule: "hourly", prompt: "x" }, null);
    expect(input).not.toHaveProperty("name");
    expect(input).not.toHaveProperty("model");
    expect(input).not.toHaveProperty("failure_deliver");
    expect(input).not.toHaveProperty("skills");
    expect(input).not.toHaveProperty("preapproved_tools");
    expect(input).not.toHaveProperty("attach_to_session");
    expect(input).not.toHaveProperty("agent_profile_id");
    expect(input).not.toHaveProperty("workspace_id");
    expect(input).not.toHaveProperty("script");
  });

  it("sends the advanced fields and a normal form of the targets", () => {
    const input = buildInput(
      {
        ...newDraft("UTC"),
        schedule: "hourly",
        prompt: "x",
        deliver: "Email:Me@Example.com",
        failureDeliver: "slack: ops",
        model: "m",
        attachToSession: true,
        skills: "a, b",
        preapprovedTools: '[{"tool":"web_fetch"}]',
      },
      null,
    );
    expect(input).toMatchObject({
      deliver: "email:me@example.com",
      failure_deliver: "slack:ops",
      model: "m",
      attach_to_session: true,
      skills: ["a", "b"],
      preapproved_tools: [{ tool: "web_fetch", limits: {} }],
    });
  });

  it("sends the script of a script job", () => {
    const input = buildInput(
      { ...newDraft("UTC"), schedule: "hourly", mode: "script", script: "echo hi" },
      null,
    );
    expect(input.mode).toBe("script");
    expect(input.script).toBe("echo hi");
    expect(input).not.toHaveProperty("prompt");
  });
});

describe("buildInput for an update", () => {
  const start = toDraft(job());

  it("sends nothing when nothing changed, also with spaces around the text", () => {
    expect(buildInput(start, start)).toEqual({});
    expect(buildInput({ ...start, name: " Digest ", prompt: " Send the digest. " }, start)).toEqual({});
  });

  it("sends only the fields that changed", () => {
    expect(buildInput({ ...start, name: "New" }, start)).toEqual({ name: "New" });
    expect(buildInput({ ...start, prompt: "Other." }, start)).toEqual({ prompt: "Other." });
    expect(buildInput({ ...start, model: "m" }, start)).toEqual({ model: "m" });
    expect(buildInput({ ...start, attachToSession: true }, start)).toEqual({ attach_to_session: true });
    expect(buildInput({ ...start, deliver: "email:a@b.co" }, start)).toEqual({ deliver: "email:a@b.co" });
    expect(buildInput({ ...start, skills: "a" }, start)).toEqual({ skills: ["a"] });
    expect(buildInput({ ...start, agentProfileId: "p2" }, start)).toEqual({ agent_profile_id: "p2" });
  });

  it("sends the zone with a new schedule, and the zone alone when only it changed", () => {
    expect(buildInput({ ...start, schedule: "daily at 8am" }, start)).toEqual({
      schedule: "daily at 8am",
      tz: "Asia/Kolkata",
    });
    expect(buildInput({ ...start, tz: "UTC" }, start)).toEqual({ tz: "UTC" });
  });

  it("sends an empty text to clear a target or the model", () => {
    const set = toDraft(job({ failure_deliver: "email:a@b.co", model: "m" }));
    expect(buildInput({ ...set, failureDeliver: "", model: "" }, set)).toEqual({
      failure_deliver: "",
      model: "",
    });
  });

  it("sends the tools when the text changed, also an empty list to clear them", () => {
    const set = toDraft(job({ preapproved_tools: [{ tool: "a", connection: null, limits: {} }] }));
    expect(buildInput({ ...set, preapprovedTools: "" }, set)).toEqual({ preapproved_tools: [] });
    expect(buildInput({ ...start, preapprovedTools: '[{"tool":"b"}]' }, start)).toEqual({
      preapproved_tools: [{ tool: "b", limits: {} }],
    });
    expect(buildInput(set, set)).toEqual({});
  });

  it("sends the script only for a script job", () => {
    const script = toDraft(job({ mode: "script", script: "echo 1", prompt: "" }));
    expect(buildInput({ ...script, script: "echo 2" }, script)).toEqual({ script: "echo 2" });
    expect(buildInput({ ...start, script: "echo" }, start)).toEqual({});
    expect(buildInput({ ...start, mode: "script", script: "echo" }, start)).toEqual({
      mode: "script",
      script: "echo",
    });
  });
});
