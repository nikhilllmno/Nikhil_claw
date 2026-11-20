"use client";

import { type RefObject, useEffect } from "react";
import { tabTarget } from "@/lib/focus";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusable(panel: HTMLElement): HTMLElement[] {
  return [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (item) => item.getClientRects().length > 0,
  );
}

/**
 * Focus rules of a modal drawer while `active`: the focus moves into the drawer, Tab stays
 * inside it, Escape closes it, and the focus goes back to where it was when it closes.
 */
export function useDrawerFocus(
  active: boolean,
  drawer: RefObject<HTMLElement | null>,
  onClose: () => void,
) {
  useEffect(() => {
    const panel = drawer.current;
    if (!active || panel === null) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // The next frame: the drawer is then visible, so the browser accepts the focus.
    const frame = requestAnimationFrame(() => focusable(panel)[0]?.focus());
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable(panel);
      const index = items.findIndex((item) => item === document.activeElement);
      const target = tabTarget(items.length, index, event.shiftKey);
      if (target !== null) {
        event.preventDefault();
        items[target]?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKey);
      opener?.focus();
    };
  }, [active, drawer, onClose]);
}
