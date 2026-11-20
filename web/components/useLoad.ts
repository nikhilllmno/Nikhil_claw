"use client";

import { useCallback, useEffect, useState } from "react";
import { errorText } from "@/lib/api";

/**
 * Loads data when `load` changes and when `reload` is called. Pass `null` to wait. `load` must
 * be stable (use `useCallback`). An answer that comes after `load` changed is dropped.
 */
export function useLoad<T>(load: (() => Promise<T>) | null, fallback: string) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (load === null) return;
    let live = true;
    load().then(
      (loaded) => {
        if (!live) return;
        setData(loaded);
        setError(null);
      },
      (e: unknown) => {
        if (live) setError(errorText(e, fallback));
      },
    );
    return () => {
      live = false;
    };
  }, [load, fallback, tick]);

  const reload = useCallback(() => {
    setError(null);
    setTick((n) => n + 1);
  }, []);
  return { data, setData, error, reload };
}
