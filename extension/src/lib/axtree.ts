// The snapshot of a tab: the accessibility tree of CDP (Accessibility.getFullAXTree) as an
// indented list, in the style of the cloud browser (Playwright "ai" mode): `- button "Pay"
// [ref=e3]`. Each element that an action can use gets a ref. The extension keeps the DOM node of
// each ref for the actions. A new snapshot makes the old refs invalid.

import { elementReason, type ElementFacts, type SensitiveCode, submitsOnEnter } from "./sensitive";

/** The part of a CDP AXNode that the snapshot uses. */
export interface AXNode {
  nodeId: string;
  ignored?: boolean;
  role?: { value?: unknown };
  name?: { value?: unknown };
  value?: { value?: unknown };
  properties?: { name: string; value?: { value?: unknown } }[];
  childIds?: string[];
  parentId?: string;
  backendDOMNodeId?: number;
}

export interface TreeResult {
  tree: string;
  /** The DOM node (backend node id) of each ref. */
  refs: Map<string, number>;
  /** The reason code of each sensitive ref. */
  sensitive: Record<string, SensitiveCode>;
  /** The refs of text fields where Enter submits a form that can change data. */
  submits: string[];
}

export const MAX_TREE_CHARS = 200_000;
export const MAX_NAME_CHARS = 200;

const ACTIONABLE = new Set([
  "button",
  "link",
  "textbox",
  "searchbox",
  "combobox",
  "checkbox",
  "radio",
  "switch",
  "slider",
  "spinbutton",
  "menuitem",
  "menuitemcheckbox",
  "menuitemradio",
  "tab",
  "option",
  "listbox",
  "treeitem",
  "textarea",
]);

/** Roles that add no meaning: their children go to the parent. */
const SKIP = new Set(["none", "generic", "presentation", "InlineTextBox", "LineBreak", "ListMarker"]);
const TEXT = new Set(["StaticText", "text"]);
const STATES = ["checked", "disabled", "expanded", "selected", "pressed", "required"];

function str(value: unknown): string {
  return typeof value === "string" ? value : typeof value === "number" || typeof value === "boolean" ? String(value) : "";
}

function clean(text: string): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > MAX_NAME_CHARS ? `${flat.slice(0, MAX_NAME_CHARS)}...` : flat;
}

function quote(text: string): string {
  return JSON.stringify(text);
}

/**
 * Builds the snapshot. `facts` gives the DOM facts of a node by backend node id (for the
 * sensitive check); `hidden` is true for the value of a node that must not be shown (a password
 * field).
 */
export function buildTree(
  nodes: AXNode[],
  facts: (backendNodeId: number) => ElementFacts | null,
  maxChars: number = MAX_TREE_CHARS,
): TreeResult {
  const byId = new Map(nodes.map((node) => [node.nodeId, node]));
  const root = nodes.find((node) => !node.parentId || !byId.has(node.parentId));
  const refs = new Map<string, number>();
  const sensitive: Record<string, SensitiveCode> = {};
  const submits: string[] = [];
  const lines: string[] = [];
  let size = 0;
  let cut = false;

  const push = (line: string) => {
    if (cut) return;
    if (size + line.length + 1 > maxChars) {
      cut = true;
      lines.push("- text: [the page is longer; the snapshot ends here]");
      return;
    }
    size += line.length + 1;
    lines.push(line);
  };

  const visit = (node: AXNode, depth: number, seen: Set<string>) => {
    if (cut || seen.has(node.nodeId)) return;
    seen.add(node.nodeId);
    const role = str(node.role?.value);
    const name = clean(str(node.name?.value));
    const children = (node.childIds ?? []).map((id) => byId.get(id)).filter((child) => child !== undefined);
    const skip = node.ignored || SKIP.has(role) || role === "" || (role === "RootWebArea" && depth > 0);
    if (TEXT.has(role)) {
      if (name) push(`${"  ".repeat(depth)}- text: ${quote(name)}`);
      return;
    }
    if (skip && !(role === "generic" && name)) {
      for (const child of children) visit(child, depth, seen);
      return;
    }
    const nodeFacts = node.backendDOMNodeId !== undefined ? facts(node.backendDOMNodeId) : null;
    const props = new Map((node.properties ?? []).map((prop) => [prop.name, prop.value?.value]));
    const focusable = props.get("focusable") === true;
    let line = `${"  ".repeat(depth)}- ${role === "RootWebArea" ? "document" : role}`;
    if (name) line += ` ${quote(name)}`;
    for (const state of STATES) {
      const value = props.get(state);
      if (value === true || value === "true") line += ` [${state}]`;
      else if (state === "checked" && value === "mixed") line += " [checked=mixed]";
    }
    const level = props.get("level");
    if (typeof level === "number") line += ` [level=${level}]`;
    const actionable = ACTIONABLE.has(role) || focusable;
    if (actionable && node.backendDOMNodeId !== undefined) {
      const ref = `e${refs.size + 1}`;
      refs.set(ref, node.backendDOMNodeId);
      line += ` [ref=${ref}]`;
      if (nodeFacts !== null) {
        const reason = elementReason({ ...nodeFacts, role: nodeFacts.role || role, name: nodeFacts.name || name });
        if (reason !== null) {
          sensitive[ref] = reason;
          line += " [needs approval]";
        }
        if (submitsOnEnter(nodeFacts)) submits.push(ref);
      }
    }
    const isPassword = nodeFacts?.tag === "input" && nodeFacts.type === "password";
    const value = clean(str(node.value?.value));
    if (value && !isPassword && value !== name && ["textbox", "searchbox", "combobox", "spinbutton", "slider"].includes(role)) {
      line += `: ${quote(value)}`;
    }
    push(line);
    for (const child of children) visit(child, depth + 1, seen);
  };

  if (root !== undefined) visit(root, 0, new Set());
  return { tree: lines.join("\n"), refs, sensitive, submits };
}

/** A DOM node of CDP DOM.getDocument (depth -1). */
export interface DomNode {
  nodeType: number;
  nodeName: string;
  backendNodeId: number;
  attributes?: string[];
  children?: DomNode[];
  shadowRoots?: DomNode[];
  contentDocument?: DomNode;
}

/**
 * The facts of each element of a DOM tree, by backend node id, from the attributes only. The
 * accessible name comes from the accessibility tree later.
 */
export function domFacts(root: DomNode): Map<number, ElementFacts> {
  const out = new Map<number, ElementFacts>();
  const forms = new Map<string, { method: string }>();
  const pending: { facts: ElementFacts; formId: string }[] = [];
  const walk = (node: DomNode, form: { method: string } | null) => {
    let current = form;
    if (node.nodeType === 1) {
      const attrs = new Map<string, string>();
      const list = node.attributes ?? [];
      for (let i = 0; i + 1 < list.length; i += 2) attrs.set((list[i] ?? "").toLowerCase(), list[i + 1] ?? "");
      const tag = node.nodeName.toLowerCase();
      const attr = (name: string) => (attrs.get(name) ?? "").trim();
      if (tag === "form") {
        current = { method: (attrs.get("method") ?? "get").toLowerCase() || "get" };
        const id = attr("id");
        if (id) forms.set(id, current);
      }
      const label = attr("aria-label");
      const facts: ElementFacts = {
        tag,
        type: tag === "input" || tag === "button" ? attr("type").toLowerCase() : "",
        role: attr("role").toLowerCase(),
        name: label || (tag === "input" ? attr("value") || attr("alt") : ""),
        autocomplete: attr("autocomplete"),
        field: [attr("name"), attr("id"), attr("placeholder"), label].join(" "),
        form: tag === "form" ? null : current,
      };
      const formId = attr("form");
      if (formId && tag !== "form") pending.push({ facts, formId });
      out.set(node.backendNodeId, facts);
    }
    for (const child of node.children ?? []) walk(child, current);
    for (const child of node.shadowRoots ?? []) walk(child, current);
  };
  walk(root, null);
  for (const { facts, formId } of pending) facts.form = forms.get(formId) ?? facts.form;
  return out;
}
