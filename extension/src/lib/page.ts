// "Ask about this page": what the extension reads from a tab. The API marks all of it as
// untrusted content (src/sama/gateway/chrome.py). The limits are the same as the API.

import type { PageContext } from "./protocol";

/** The API cuts the text at 20 000 characters. Send a little more, so that it knows. */
export const MAX_PAGE_TEXT = 20_001;
export const MAX_TITLE = 300;
export const MAX_URL = 2_048;
export const MAX_SCREENSHOT = 5_000_000;
export const MAX_QUESTION = 8_000;
export const DEFAULT_QUESTION = "Summarize this page.";

/** Why a page cannot be read, or `null` for a web page. */
export function pageProblem(url: string | undefined): string | null {
  if (!url) return "sama cannot see this tab. Open a web page.";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "sama cannot read this page.";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "sama reads only http and https pages.";
  }
  if (url.length > MAX_URL) return "The address of this page is too long.";
  if (parsed.hostname === "chromewebstore.google.com" || parsed.hostname === "chrome.google.com") {
    return "Chrome does not let extensions read the Chrome Web Store.";
  }
  return null;
}

/** The origin pattern for the site access request: `https://example.com/*`. */
export function originPattern(url: string): string {
  return `${new URL(url).origin}/*`;
}

/** What the script in the tab returns. */
export interface PageRead {
  url: string;
  title: string;
  text: string;
  selection: string;
}

/**
 * Runs in the tab (chrome.scripting.executeScript). It must not use anything from outside the
 * function. It reads only the visible text: no form values, no cookies, no storage.
 */
export function readPage(maxText: number): PageRead {
  const selection = String(globalThis.getSelection?.() ?? "").trim();
  const body = document.body;
  const text = body ? body.innerText : (document.documentElement?.textContent ?? "");
  return {
    url: location.href,
    title: document.title,
    text: text.slice(0, maxText),
    selection: selection.slice(0, maxText),
  };
}

/** Collapses runs of blank lines and spaces at line ends. */
export function tidy(text: string): string {
  return text
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** The page frame for the API. A selection comes first, because the user chose it. */
export function pageContext(read: PageRead, screenshot: string | null): PageContext {
  const selection = tidy(read.selection);
  const body = tidy(read.text);
  const text = selection ? `Selected text:\n${selection}\n\nPage text:\n${body}` : body;
  return {
    url: read.url.slice(0, MAX_URL),
    title: read.title.replace(/\s+/g, " ").trim().slice(0, MAX_TITLE),
    text: text.slice(0, MAX_PAGE_TEXT),
    screenshot: screenshot !== null && screenshot.length <= MAX_SCREENSHOT ? screenshot : null,
  };
}

/** The question that goes with a page. An empty composer asks for a summary. */
export function pageQuestion(text: string): string {
  const question = text.trim();
  return question === "" ? DEFAULT_QUESTION : question;
}

/** The start of the page block in a message (src/sama/gateway/chrome.py, HEADER). */
export const PAGE_HEADER = "[The user shared the page below from their browser.";

/** A user message with a page: the question and the page address, for a short view. */
export function splitPageMessage(text: string): { question: string; url: string | null } {
  const start = text.indexOf(PAGE_HEADER);
  if (start < 0) return { question: text, url: null };
  const match = /<untrusted_content source="chrome_page" url="([^"]*)">/.exec(text.slice(start));
  const question = text
    .slice(0, start)
    .split("\n")
    .filter((line) => !line.startsWith("[image attached:"))
    .join("\n")
    .trim();
  const url = unescapeAttribute(match?.[1] ?? "");
  return { question, url };
}

/** Reverses Python `html.escape(value, quote=True)`. */
function unescapeAttribute(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}
