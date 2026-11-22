"use client";

import { useSyncExternalStore } from "react";

/** True while the media query matches, for example `(max-width: 75rem)`. False on the server. */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}
