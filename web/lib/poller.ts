// A single-flight poller for the Code page. It never has two requests open at once. The next poll
// starts `intervalMs` after the current request settles, so a slow answer is not dropped. A manual
// `refresh` while a request is open does not start a second one: it asks for one more run right
// after the open one. `stop` ends the poller. An answer that comes after `stop` is ignored.

export interface PollerOptions<T> {
  load: () => Promise<T>;
  onData: (data: T) => void;
  onError: (error: unknown) => void;
  intervalMs: number;
  /** False: skip the request this time (the page is hidden). The poller tries again later. */
  visible?: () => boolean;
}

export interface Poller {
  /** Starts the first request now. */
  start: () => void;
  /** Loads now, or right after the open request. Many calls in a row give one extra run. */
  refresh: () => void;
  /** Ends the poller. Open requests are ignored, and no timer stays. */
  stop: () => void;
}

export function createPoller<T>(options: PollerOptions<T>): Poller {
  let stopped = false;
  let inFlight = false;
  let again = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const clear = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };

  const schedule = () => {
    clear();
    if (stopped) return;
    timer = setTimeout(() => {
      timer = null;
      if (options.visible && !options.visible()) schedule();
      else run();
    }, options.intervalMs);
  };

  const run = () => {
    if (stopped) return;
    if (inFlight) {
      again = true;
      return;
    }
    clear();
    inFlight = true;
    again = false;
    let promise: Promise<T>;
    try {
      promise = options.load();
    } catch (e) {
      promise = Promise.reject(e);
    }
    promise.then(
      (data) => {
        if (!stopped) options.onData(data);
      },
      (error: unknown) => {
        if (!stopped) options.onError(error);
      },
    ).finally(() => {
      inFlight = false;
      if (stopped) return;
      if (again) run();
      else schedule();
    });
  };

  return {
    start: run,
    refresh: run,
    stop: () => {
      stopped = true;
      again = false;
      clear();
    },
  };
}
