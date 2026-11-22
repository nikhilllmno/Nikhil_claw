// Steps in the web app that many tests share: start a session, send a message, wait for the
// answer of the agent, and decide an approval.

import { expect, type Locator, type Page } from "@playwright/test";

/** A model can take a while, and a first sandbox start too. */
export const REPLY_TIMEOUT_MS = 4 * 60_000;

/** Starts a session from the New session form and returns its id. */
export async function startSession(
  page: Page,
  title: string,
  options: { audience?: "private" | "shared"; agentId?: string } = {},
): Promise<string> {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "What should we work on?" })).toBeVisible();
  if (options.agentId) {
    await page.getByRole("combobox", { name: /^Agent/ }).selectOption(options.agentId);
  }
  if (options.audience) {
    await page.getByRole("combobox", { name: /^Who can see it/ }).selectOption(options.audience);
  }
  await page.getByLabel("Title (optional)").fill(title);
  await page.getByRole("button", { name: "Start session" }).click();
  await page.waitForURL(/\/s\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
  const id = page.url().split("/s/")[1];
  if (!id) throw new Error(`No session id in ${page.url()}`);
  return id;
}

export function composer(page: Page): Locator {
  return page.getByRole("textbox", { name: "Message" });
}

/** Types a message (or a slash command) and sends it with the Send button. */
export async function send(page: Page, text: string): Promise<void> {
  await composer(page).fill(text);
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(composer(page)).toHaveValue("");
}

/** The saved answers of the agent (not the live one). */
export function replies(page: Page): Locator {
  return page.getByRole("article", { name: "Assistant", exact: true });
}

/** Waits until a saved answer of the agent has `text`. */
export async function waitForReply(page: Page, text: string | RegExp, timeout = REPLY_TIMEOUT_MS) {
  await expect(replies(page).filter({ hasText: text }).first()).toBeVisible({ timeout });
}

/** Waits until no run of the session is open: the Working badge is gone. */
export async function waitIdle(page: Page, timeout = REPLY_TIMEOUT_MS): Promise<void> {
  // "Stop all" when more than one run is open (M7.1).
  await expect(page.getByRole("button", { name: /^Stop( all)?$/ })).toHaveCount(0, { timeout });
}

/** The approval card for a tool. */
export function approvalCard(page: Page, tool: string): Locator {
  return page.getByRole("region", { name: `Approval for ${tool}` });
}

/** Waits for the approval card of `tool` and presses a button on it. */
export async function decide(
  page: Page,
  tool: string,
  button: "Approve" | "Always allow" | "Deny",
  timeout = REPLY_TIMEOUT_MS,
): Promise<void> {
  const card = approvalCard(page, tool);
  await expect(card).toBeVisible({ timeout });
  await expect(card.getByRole("heading", { name: `The agent wants to use ${tool}` })).toBeVisible();
  await card.getByRole("button", { name: button, exact: true }).click();
  await expect(card).toHaveCount(0);
}

/**
 * Approves the first approval card and waits until that card is gone. A next card can come at
 * once, so the wait follows the element of this card, not the locator. Returns the tool name.
 */
async function approveFirstCard(page: Page): Promise<string> {
  const card = page.getByRole("region", { name: /^Approval for / }).first();
  const tool = ((await card.getAttribute("aria-label")) ?? "").replace("Approval for ", "");
  const element = await card.elementHandle();
  await card.getByRole("button", { name: "Approve", exact: true }).click();
  await element?.waitForElementState("hidden", { timeout: 20_000 });
  return tool;
}

/**
 * Waits for an answer with `text`, and approves each tool call that asks on the way. Use it
 * when the policy of a tool can be `ask` or `allow`, for example the tools of a new connection.
 */
export async function waitForReplyApproving(
  page: Page,
  text: string | RegExp,
  timeout = REPLY_TIMEOUT_MS,
): Promise<string[]> {
  const approved: string[] = [];
  const deadline = Date.now() + timeout;
  const answer = replies(page).filter({ hasText: text }).first();
  const card = page.getByRole("region", { name: /^Approval for / }).first();
  while (Date.now() < deadline) {
    if (await answer.isVisible()) return approved;
    if (await card.isVisible()) {
      approved.push(await approveFirstCard(page));
      continue;
    }
    await answer.or(card).waitFor({ state: "visible", timeout: 5_000 }).catch(() => undefined);
  }
  await expect(answer).toBeVisible({ timeout: 1 });
  return approved;
}

/**
 * Approves each approval card until no run of the session is open. Use it for a run that makes
 * an unknown number of tool calls that ask, for example the work after an approved plan.
 */
export async function approveUntilIdle(page: Page, timeout = REPLY_TIMEOUT_MS * 2): Promise<string[]> {
  const approved: string[] = [];
  const deadline = Date.now() + timeout;
  const card = page.getByRole("region", { name: /^Approval for / }).first();
  const stop = page.getByRole("button", { name: /^Stop( all)?$/ });
  let quiet = 0;
  while (Date.now() < deadline) {
    if (await card.isVisible()) {
      approved.push(await approveFirstCard(page));
      quiet = 0;
      continue;
    }
    // Idle twice in a row, so a short gap between two runs does not end the wait.
    quiet = (await stop.count()) === 0 ? quiet + 1 : 0;
    if (quiet >= 3) return approved;
    await card.waitFor({ state: "visible", timeout: 3_000 }).catch(() => undefined);
  }
  throw new Error(`The session was not idle after ${timeout} ms.`);
}
