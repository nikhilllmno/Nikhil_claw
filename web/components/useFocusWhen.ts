"use client";

import { type RefObject, useEffect, useRef } from "react";

/**
 * Moves the focus when a form or a view opens or closes. When `active` becomes true, the focus
 * goes to `target`. When it becomes false again, the focus goes to `back`. A view that is
 * active on its first render gets the focus on mount. Both elements must exist when the
 * change commits.
 */
export function useFocusWhen(
  active: boolean,
  target: RefObject<HTMLElement | null>,
  back?: RefObject<HTMLElement | null>,
) {
  const was = useRef(false);
  useEffect(() => {
    if (active && !was.current) target.current?.focus();
    else if (!active && was.current) back?.current?.focus();
    was.current = active;
  }, [active, target, back]);
}
