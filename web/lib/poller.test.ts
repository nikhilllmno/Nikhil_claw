import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPoller } from "./poller";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("createPoller", () => {
  it("never has two requests open, and delivers a slow answer", async () => {
    const pending: ReturnType<typeof deferred<number>>[] = [];
    const load = vi.fn(() => {
      const d = deferred<number>();
      pending.push(d);
      return d.promise;
    });
    const onData = vi.fn();
    const poller = createPoller({ load, onData, onError: vi.fn(), intervalMs: 1000 });
    poller.start();
    expect(load).toHaveBeenCalledTimes(1);
    // The request takes 3.5 intervals. No second request starts meanwhile.
    await vi.advanceTimersByTimeAsync(3500);
    expect(load).toHaveBeenCalledTimes(1);
    pending[0]?.resolve(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(onData).toHaveBeenCalledWith(1);
    // The next request starts one interval after the answer.
    await vi.advanceTimersByTimeAsync(999);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(2);
    pending[1]?.resolve(2);
    await vi.advanceTimersByTimeAsync(0);
    expect(onData).toHaveBeenLastCalledWith(2);
    poller.stop();
  });

  it("coalesces manual refreshes while a request is open", async () => {
    const pending: ReturnType<typeof deferred<number>>[] = [];
    const load = vi.fn(() => {
      const d = deferred<number>();
      pending.push(d);
      return d.promise;
    });
    const poller = createPoller({ load, onData: vi.fn(), onError: vi.fn(), intervalMs: 1000 });
    poller.start();
    poller.refresh();
    poller.refresh();
    poller.refresh();
    expect(load).toHaveBeenCalledTimes(1);
    pending[0]?.resolve(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(load).toHaveBeenCalledTimes(2); // one extra run, right after the first
    pending[1]?.resolve(2);
    await vi.advanceTimersByTimeAsync(0);
    expect(load).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("goes on after an error", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce(5);
    const onData = vi.fn();
    const onError = vi.fn();
    const poller = createPoller({ load, onData, onError, intervalMs: 1000 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onData).toHaveBeenCalledWith(5);
    poller.stop();
  });

  it("skips a request while the page is hidden", async () => {
    let visible = false;
    const load = vi.fn().mockResolvedValue(1);
    const poller = createPoller({ load, onData: vi.fn(), onError: vi.fn(), intervalMs: 1000, visible: () => visible });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(load).toHaveBeenCalledTimes(1);
    visible = true;
    await vi.advanceTimersByTimeAsync(1000);
    expect(load).toHaveBeenCalledTimes(2);
    poller.stop();
  });

  it("ignores an answer after stop and leaves no timer", async () => {
    const d = deferred<number>();
    const load = vi.fn(() => d.promise);
    const onData = vi.fn();
    const poller = createPoller({ load, onData, onError: vi.fn(), intervalMs: 1000 });
    poller.start();
    poller.stop();
    d.resolve(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(onData).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    poller.refresh();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("clears the timer when stopped between polls", async () => {
    const load = vi.fn().mockResolvedValue(1);
    const poller = createPoller({ load, onData: vi.fn(), onError: vi.fn(), intervalMs: 1000 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(1);
    poller.stop();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(5000);
    expect(load).toHaveBeenCalledTimes(1);
  });
});
