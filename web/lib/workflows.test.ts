import { describe, expect, it } from "vitest";
import {
  MAX_YAML_BYTES,
  WORKFLOW_TEMPLATE,
  canCancelInstance,
  canStart,
  instanceStatusLabel,
  instanceStatusTone,
  parseInputs,
  pauseAction,
  validateYamlText,
  validationNotes,
  workflowStatusLabel,
  workflowStatusTone,
} from "./workflows";

describe("labels", () => {
  it("names the status of a workflow", () => {
    expect(workflowStatusLabel("active")).toBe("Active");
    expect(workflowStatusLabel("paused")).toBe("Paused");
    expect(workflowStatusLabel("pending_approval")).toBe("Needs approval");
    expect(workflowStatusLabel("removed")).toBe("Removed");
    expect(workflowStatusTone("active")).toBe("accent");
    expect(workflowStatusTone("pending_approval")).toBe("warning");
    expect(workflowStatusTone("paused")).toBe("neutral");
  });

  it("names the status of an instance", () => {
    expect(instanceStatusLabel("running")).toBe("Running");
    expect(instanceStatusLabel("waiting")).toBe("Waiting");
    expect(instanceStatusLabel("completed")).toBe("Completed");
    expect(instanceStatusLabel("failed")).toBe("Failed");
    expect(instanceStatusLabel("cancelled")).toBe("Cancelled");
    expect(instanceStatusLabel("other")).toBe("other");
    expect(instanceStatusTone("completed")).toBe("accent");
    expect(instanceStatusTone("failed")).toBe("danger");
    expect(instanceStatusTone("waiting")).toBe("warning");
    expect(instanceStatusTone("running")).toBe("neutral");
  });
});

describe("actions", () => {
  it("cancels only an instance that has not ended", () => {
    expect(canCancelInstance("running")).toBe(true);
    expect(canCancelInstance("waiting")).toBe(true);
    expect(canCancelInstance("completed")).toBe(false);
    expect(canCancelInstance("cancelled")).toBe(false);
  });

  it("offers pause or resume", () => {
    expect(pauseAction("active")).toBe("pause");
    expect(pauseAction("paused")).toBe("resume");
    expect(pauseAction("pending_approval")).toBeNull();
    expect(pauseAction("removed")).toBeNull();
  });

  it("starts an active workflow only", () => {
    expect(canStart("active")).toBe(true);
    expect(canStart("paused")).toBe(false);
    expect(canStart("pending_approval")).toBe(false);
  });
});

describe("text checks", () => {
  it("needs YAML text of a fitting size", () => {
    expect(validateYamlText(" ")).toContain("Write the YAML");
    expect(validateYamlText(WORKFLOW_TEMPLATE)).toBeNull();
    expect(validateYamlText("a".repeat(MAX_YAML_BYTES + 1))).toContain("50,000 bytes");
  });

  it("has a template with a name and steps", () => {
    expect(WORKFLOW_TEMPLATE).toContain("name:");
    expect(WORKFLOW_TEMPLATE).toContain("steps:");
  });

  it("parses the inputs of a start", () => {
    expect(parseInputs("")).toEqual({ ok: true, inputs: {} });
    expect(parseInputs('{"topic":"x"}')).toEqual({ ok: true, inputs: { topic: "x" } });
    expect(parseInputs("[]")).toMatchObject({ ok: false });
    expect(parseInputs("{")).toEqual({ ok: false, error: "The inputs field is not valid JSON." });
  });

  it("writes the notes of a check", () => {
    expect(validationNotes({ needs_approval: false, approval_reasons: ["x"] })).toEqual([]);
    expect(validationNotes({ needs_approval: true, approval_reasons: ["it sends to slack:#ops."] })).toEqual([
      "The owner must approve this version before it can run.",
      "It sends to slack:#ops.",
    ]);
  });
});
