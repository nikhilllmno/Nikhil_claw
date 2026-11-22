// The keys of an approval card, as in the permission prompt of Claude Code: 1 allows once,
// 2 allows always, 3 denies. Pure, so tests can run it.

export type ApprovalAction = "approve" | "always" | "deny";

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
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "repeat">,
  active: FocusLike | null | undefined,
  allowAlways: boolean,
): ApprovalAction | null {
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return null;
  if (typingInField(active)) return null;
  switch (event.key) {
    case "1":
      return "approve";
    case "2":
      return allowAlways ? "always" : null;
    case "3":
      return "deny";
    default:
      return null;
  }
}
