// Focus rules for a modal panel, as pure functions so that tests can run them.

/**
 * Where Tab (or Shift+Tab) must send the focus in a panel with `count` focusable items.
 * `index` is the item that has the focus now, or -1 if the focus is outside the panel.
 * Returns the index to focus, or `null` if the browser may move the focus by itself.
 */
export function tabTarget(count: number, index: number, backwards: boolean): number | null {
  if (count === 0) return null;
  if (index < 0) return backwards ? count - 1 : 0;
  if (backwards && index === 0) return count - 1;
  if (!backwards && index === count - 1) return 0;
  return null;
}
