// The keys of an approval card, as in the permission prompt of Claude Code: 1 allows once,
// 2 allows for this session, 3 denies. "Always allow" has no key. Pure, so tests can run it.

export type ApprovalAction = "approve" | "session" | "always" | "deny";

/** The id of the message box of the chat. */
export const MESSAGE_FIELD_ID = "message";

interface FocusLike {
  tagName?: string;
  id?: string;
  isContentEditable?: boolean;
  value?: string;
}

/**
 * True when a key press is text for a field: the focus is in an input, a select, or a text
 * area, except the message box when it is empty.
 */
export function typingInField(active: FocusLike | null | undefined): boolean {
  if (!active) return false;
  if (active.isContentEditable) return true;
  const tag = (active.tagName ?? "").toUpperCase();
  if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") return false;
  return !(active.id === MESSAGE_FIELD_ID && (active.value ?? "") === "");
}

/** The action of a key press on the active approval card, or `null`. */
export function approvalKey(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "repeat"> &
    Partial<Pick<KeyboardEvent, "defaultPrevented" | "isComposing">>,
  active: FocusLike | null | undefined,
  allowAlways: boolean,
): ApprovalAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return null;
  if (event.defaultPrevented || event.isComposing) return null;
  if (typingInField(active)) return null;
  switch (event.key) {
    case "1":
      return "approve";
    case "2":
      return allowAlways ? "session" : null;
    case "3":
      return "deny";
    default:
      return null;
  }
}

interface ElementLike extends FocusLike {
  closest?: (selector: string) => unknown;
}

/** True when the chat is behind a modal (an open dialog, or the drawer that makes it inert). */
export function behindModal(chat: ElementLike | null | undefined, doc: { querySelector: (s: string) => unknown }): boolean {
  if (doc.querySelector("dialog[open], [aria-modal='true']")) return true;
  return Boolean(chat?.closest?.("[inert]"));
}

/**
 * What Esc does in the chat: `stop` the open runs, or `ignore` it because a field other than
 * the message box has the focus, a menu is open, or a modal covers the chat.
 */
export function escapeTarget(target: unknown, chat: ElementLike | null | undefined, doc: Document | null = typeof document === "undefined" ? null : document): "stop" | "ignore" {
  if (doc !== null) {
    if (behindModal(chat, doc)) return "ignore";
    if (doc.querySelector("details[data-menu][open]")) return "ignore";
  }
  const field = target as FocusLike | null;
  if (field && typingInField(field) && field.id !== MESSAGE_FIELD_ID) return "ignore";
  if (field?.isContentEditable) return "ignore";
  return "stop";
}
