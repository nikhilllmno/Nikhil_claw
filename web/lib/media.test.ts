import { describe, expect, it, vi } from "vitest";
import type { MediaItem } from "./api";
import { formatBytes, hasPreview, listAllMedia, MEDIA_MAX_PAGES, mediaByRun, mediaLabel, mediaName } from "./media";

function item(over: Partial<MediaItem>): MediaItem {
  return {
    id: "m1",
    run_id: "r1",
    kind: "file",
    mime_type: "text/csv",
    name: "report.csv",
    size_bytes: 1229,
    caption: "",
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("media", () => {
  it("names a file, with a fallback for old rows", () => {
    expect(mediaName(item({}))).toBe("report.csv");
    expect(mediaName(item({ kind: "image", name: "" }))).toBe("image.png");
    expect(mediaName(item({ kind: "audio", name: " " }))).toBe("voice.ogg");
    expect(mediaName(item({ name: "" }))).toBe("file");
  });

  it("formats a size", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(1229)).toBe("1.2 KB");
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(3.5 * 1024 * 1024)).toBe("3.5 MB");
    expect(formatBytes(-1)).toBe("");
  });

  it("labels the type and the size", () => {
    expect(mediaLabel(item({}))).toBe("CSV · 1.2 KB");
    expect(mediaLabel(item({ kind: "image", name: "", size_bytes: 100 }))).toBe("PNG · 100 B");
    expect(mediaLabel(item({ name: "Makefile" }))).toBe("File · 1.2 KB");
  });

  it("groups the files by run, the oldest first", () => {
    const items = [item({ id: "c", run_id: "r2" }), item({ id: "b" }), item({ id: "a" })];
    const byRun = mediaByRun(items);
    expect(byRun.get("r1")?.map((m) => m.id)).toEqual(["a", "b"]);
    expect(byRun.get("r2")?.map((m) => m.id)).toEqual(["c"]);
    expect(byRun.size).toBe(2);
  });

  it("reads pages until a page is short", async () => {
    const full = Array.from({ length: 100 }, (_, i) => item({ id: `m${i}` }));
    const page = vi.fn(async (_limit: number, offset: number) => (offset < 200 ? full : [item({ id: "last" })]));
    const all = await listAllMedia(page);
    expect(all).toHaveLength(201);
    expect(page.mock.calls.map((call) => call[1])).toEqual([0, 100, 200]);
  });

  it("stops after the page limit and rejects when a page fails", async () => {
    const full = Array.from({ length: 100 }, () => item({}));
    const endless = vi.fn(async () => full);
    expect(await listAllMedia(endless)).toHaveLength(100 * MEDIA_MAX_PAGES);
    expect(endless).toHaveBeenCalledTimes(MEDIA_MAX_PAGES);
    await expect(listAllMedia(async () => Promise.reject(new Error("down")))).rejects.toThrow("down");
  });

  it("shows a preview for images and for safe picture types", () => {
    expect(hasPreview(item({ kind: "image", mime_type: "image/png" }))).toBe(true);
    expect(hasPreview(item({ mime_type: "image/jpeg" }))).toBe(true);
    expect(hasPreview(item({ mime_type: "image/WEBP" }))).toBe(true);
    expect(hasPreview(item({ mime_type: "image/svg+xml" }))).toBe(false);
    expect(hasPreview(item({ mime_type: "text/csv" }))).toBe(false);
  });
});
