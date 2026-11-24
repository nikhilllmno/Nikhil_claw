// The files that a run made, shown under its answer in the chat. Pure, so tests can run it.

import type { MediaItem } from "./api";

const KINDS = { image: "Image", audio: "Audio", file: "File" } as const;
const FALLBACK_NAMES = { image: "image.png", audio: "voice.ogg", file: "file" } as const;

/** The download name of a file. Old image and audio rows have no name. */
export function mediaName(item: MediaItem): string {
  return item.name.trim() || FALLBACK_NAMES[item.kind];
}

/** A size as short text: 512 B, 1.2 KB, 3.4 MB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1).replace(/\.0$/, "")} ${units[unit]}`;
}

/** The type and the size of a file as one label, for example `CSV · 1.2 KB`. */
export function mediaLabel(item: MediaItem): string {
  const extension = /\.([A-Za-z0-9]{1,5})$/.exec(mediaName(item))?.[1];
  const type = extension ? extension.toUpperCase() : KINDS[item.kind];
  return [type, formatBytes(item.size_bytes)].filter(Boolean).join(" · ");
}

/** The files by run, the oldest first. The API sends the newest first. */
export function mediaByRun(items: readonly MediaItem[]): Map<string, MediaItem[]> {
  const byRun = new Map<string, MediaItem[]>();
  for (const item of [...items].reverse()) {
    const list = byRun.get(item.run_id);
    if (list === undefined) byRun.set(item.run_id, [item]);
    else list.push(item);
  }
  return byRun;
}
