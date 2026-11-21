// Sensitive actions (docs/architecture.md, Section 14.2.2): submit a form that changes data,
// buy or pay, send, delete, confirm, and type into a password or payment field. Such an action
// always waits for a click of the user. The API asks through the approval card of the tool gate
// from the codes of the latest snapshot; the extension checks the live element again before it
// acts and asks the user itself when nobody approved the call.

import { parseKey } from "./keys";

/** What the extension knows about an element. */
export interface ElementFacts {
  /** The tag name in lower case (`button`, `input`, `a`). */
  tag: string;
  /** The `type` attribute of an input or a button, in lower case. */
  type: string;
  /** The ARIA role (from the accessibility tree or the `role` attribute). */
  role: string;
  /** The accessible name or the visible text. */
  name: string;
  /** The `autocomplete` attribute. */
  autocomplete: string;
  /** The `name`, `id`, `placeholder`, and `aria-label` of a field, joined. */
  field: string;
  /** The form of the element, or `null`. */
  form: { method: string } | null;
}

/** The codes of the reasons. The API has a fixed text for each (src/sama/browser/chrome.py). */
export type SensitiveCode = "submit" | "pay" | "send" | "delete" | "confirm" | "password" | "payment";

export const REASON_TEXT: Record<SensitiveCode, string> = {
  submit: "submit a form",
  pay: "buy or pay",
  send: "send or publish",
  delete: "delete or remove",
  confirm: "confirm",
  password: "type into a password field",
  payment: "type into a payment field",
};

const KEYWORDS: readonly [RegExp, SensitiveCode][] = [
  [/\b(pay|purchase|buy|place (my |your )?order|order now|complete (my |your )?(order|purchase)|checkout|check out|donate|subscribe|book now)\b/i, "pay"],
  [/\b(send|publish|post|reply|tweet|share|invite)\b/i, "send"],
  [/\b(delete|remove|erase|destroy|discard|cancel (my )?(account|subscription|order))\b/i, "delete"],
  [/\b(transfer|withdraw|confirm|authorize|authorise|approve|sign)\b/i, "confirm"],
];

const PAYMENT_FIELD = /(card.?(number|num|no)|cc.?(number|num|no)|credit.?card|cvv|cvc|csc|security.?code|expir|iban|routing.?number|account.?number)/i;

const CLICKABLE_ROLES = new Set(["button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "option"]);
const TEXT_INPUT_TYPES = new Set(["", "text", "email", "number", "password", "search", "tel", "url", "date", "datetime-local", "month", "time", "week"]);

/** True for a form that can change data: a form with a method other than GET. */
function writes(form: ElementFacts["form"]): boolean {
  return form !== null && form.method.toLowerCase() !== "get";
}

function isSubmitControl(facts: ElementFacts): boolean {
  if (facts.tag === "button") return facts.type === "" || facts.type === "submit";
  if (facts.tag === "input") return facts.type === "submit" || facts.type === "image";
  return false;
}

/** True for a field where the user types text. */
export function isTextField(facts: ElementFacts): boolean {
  if (facts.tag === "textarea") return true;
  if (facts.tag !== "input") return facts.role === "textbox" || facts.role === "searchbox" || facts.role === "combobox";
  return TEXT_INPUT_TYPES.has(facts.type);
}

/** Why typing into the element is sensitive, or `null`. */
export function typeReason(facts: ElementFacts): SensitiveCode | null {
  if (facts.tag === "input" && facts.type === "password") return "password";
  if (/^cc-/.test(facts.autocomplete.trim().toLowerCase()) || PAYMENT_FIELD.test(facts.field)) return "payment";
  return null;
}

/** Why a click on the element is sensitive, or `null`. */
export function clickReason(facts: ElementFacts): SensitiveCode | null {
  const clickable = CLICKABLE_ROLES.has(facts.role) || facts.tag === "button" || facts.tag === "a" || isSubmitControl(facts);
  if (!clickable) return null;
  for (const [pattern, code] of KEYWORDS) if (pattern.test(facts.name)) return code;
  if (isSubmitControl(facts) && writes(facts.form)) return "submit";
  return null;
}

/** True for a text field where Enter submits a form that can change data. */
export function submitsOnEnter(facts: ElementFacts): boolean {
  return facts.tag === "input" && isTextField(facts) && writes(facts.form);
}

/** The reason of an element for the snapshot: a click reason, else a typing reason. */
export function elementReason(facts: ElementFacts): SensitiveCode | null {
  return clickReason(facts) ?? (isTextField(facts) ? typeReason(facts) : null);
}

/** The arguments of an action that matter for the check. */
export interface ActFacts {
  action: string;
  submit: boolean;
  key: string | null;
}

/**
 * Why an action on an element is sensitive, or `null`. `target` is the element of the ref, or the
 * focused element for a key press without a ref.
 */
export function actionReason(act: ActFacts, target: ElementFacts | null): SensitiveCode | null {
  if (target === null) return null;
  switch (act.action) {
    case "click":
      return clickReason(target);
    case "type":
      return typeReason(target) ?? (act.submit && submitsOnEnter(target) ? "submit" : null);
    case "select":
      return typeReason(target);
    case "press": {
      // The same parser as the key that the extension sends (lib/keys.ts).
      const key = parseKey(act.key ?? "");
      if (key === null) return null;
      if ((key.key === "Enter" || key.key === " ") && clickReason(target) !== null) return clickReason(target);
      if (key.key === "Enter" && submitsOnEnter(target)) return "submit";
      // A key that types text into a password or payment field.
      if (key.text && key.key !== "Enter" && isTextField(target)) return typeReason(target);
      return null;
    }
    default:
      return null;
  }
}

/**
 * Runs in the page (Runtime.callFunctionOn with the element as `this`). It must not use anything
 * from outside the function. It reads attributes only, never the value of a field.
 */
export function elementFacts(this: Element): ElementFacts {
  const attr = (name: string) => (this.getAttribute(name) ?? "").trim();
  const tag = this.tagName.toLowerCase();
  const control = this as HTMLInputElement;
  const form = "form" in control && control.form ? control.form : this.closest("form");
  const label = attr("aria-label");
  const text = ((this as HTMLElement).innerText ?? this.textContent ?? "").trim();
  const value = tag === "input" && ["submit", "button", "image"].includes(attr("type").toLowerCase()) ? attr("value") || attr("alt") : "";
  return {
    tag,
    type: tag === "input" || tag === "button" ? attr("type").toLowerCase() : "",
    role: attr("role").toLowerCase(),
    name: (label || value || text).replace(/\s+/g, " ").slice(0, 200),
    autocomplete: attr("autocomplete"),
    field: [attr("name"), attr("id"), attr("placeholder"), label].join(" "),
    form: form ? { method: (form.getAttribute("method") ?? "get").toLowerCase() } : null,
  };
}
