"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "@/lib/api";
import { createPoller, type Poller } from "@/lib/poller";

/** How often the Code page reads new data while it is open. */
export const POLL_MS = 5_000;

/**
 * Like `useLoad`, and it loads again `intervalMs` after each request settles while the page is
 * visible. It has one request open at most, and `reload` while a request is open adds one run
 * after it. The data stays on screen during a new load, so the view does not flicker. `load` must
 * be stable (use `useCallback`).
 */
export function usePolled<T>(load: (() => Promise<T>) | null, fallback: string, intervalMs = POLL_MS) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const poller = useRef<Poller | null>(null);

  useEffect(() => {
    if (load === null) return;
    const next = createPoller<T>({
      load,
      intervalMs,
      visible: () => !document.hidden,
      onData: (loaded) => {
        setData(loaded);
        setError(null);
      },
      onError: (e) => setError(errorText(e, fallback)),
    });
    poller.current = next;
    next.start();
    return () => {
      next.stop();
      if (poller.current === next) poller.current = null;
    };
  }, [load, fallback, intervalMs]);

  const reload = useCallback(() => {
    setError(null);
    poller.current?.refresh();
  }, []);
  return { data, setData, error, reload };
}

/** The time now in ms. It changes every `intervalMs`, so that "5 min ago" stays right. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
