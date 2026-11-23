// Images in the message box and in the chat (M8.2). Pure, so tests can run it.

import { IMAGE_TYPES, MAX_IMAGES_PER_MESSAGE } from "./api";

const MARKER = /^\[image attached: \S+ [^\n]*; use vision_analyze to look at it\]$/;

/** The text of a message without the marker lines that the API adds for its images. */
export function withoutImageMarkers(text: string): string {
  return text
    .split("\n")
    .filter((line) => !MARKER.test(line.trim()))
    .join("\n")
    .trim();
}

/** The image files in a list (a paste, a drop, or a file input). Other files stay out. */
export function imageFiles(files: Iterable<File>): File[] {
  return [...files].filter((file) => (IMAGE_TYPES as readonly string[]).includes(file.type));
}

/** The files after an add: the old files, then the new ones, at most the limit of a message. */
export function addImages(current: readonly File[], added: readonly File[]): { files: File[]; dropped: number } {
  const all = [...current, ...added];
  const files = all.slice(0, MAX_IMAGES_PER_MESSAGE);
  return { files, dropped: all.length - files.length };
}
