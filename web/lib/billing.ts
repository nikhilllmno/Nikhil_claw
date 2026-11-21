// Billing and quotas (docs/architecture.md, Section 17.5): the rules that the Billing page shows.
// The API checks every rule again; these only choose what the page offers.

import { ApiError, type BillingOverview, type BillingPlan } from "./api";

/** A US dollar amount for the page, for example `"USD 20.00"`. `null` gives `fallback`. */
export function usd(
  value: string | null | undefined,
  fallback = "No limit",
): string {
  if (value === null || value === undefined || value === "") return fallback;
  const amount = Number(value);
  return Number.isFinite(amount) ? `USD ${amount.toFixed(2)}` : fallback;
}

/** The part of a quota that is used, from 0 to 100. `null` when there is no quota. */
export function usedPercent(
  used: string | null,
  cap: string | null,
): number | null {
  if (cap === null) return null;
  const limit = Number(cap);
  const spent = Number(used ?? "0");
  if (!Number.isFinite(limit) || !Number.isFinite(spent)) return null;
  if (limit <= 0) return 100;
  return Math.min(100, Math.max(0, Math.round((spent / limit) * 100)));
}

export type PlanAction = "current" | "buy" | "change" | "none";

/** What the page offers for one paid plan of the price list. */
export function planAction(
  overview: BillingOverview,
  plan: BillingPlan,
): PlanAction {
  if (plan.name === overview.plan.name) return "current";
  if (!plan.paid) return "none";
  if (overview.can_change) return "change";
  if (overview.can_buy) return "buy";
  return "none";
}

/** The return from Stripe: `?org=<id>` and, after Checkout, `checkout=success` or `cancel`. */
export function stripeReturn(search: string): {
  org: string | null;
  checkout: "success" | "cancel" | null;
} {
  const params = new URLSearchParams(search);
  const value = params.get("checkout");
  const org = params.get("org");
  return {
    org: org !== null && /^[0-9a-f-]{36}$/i.test(org) ? org : null,
    checkout: value === "success" || value === "cancel" ? value : null,
  };
}

/** The page asks the API again this many times while it waits for the Stripe webhook. */
export const CONFIRM_ATTEMPTS = 10;
export const CONFIRM_INTERVAL_MS = 3000;

/** The notice after Checkout. `paid`: the org has a paid plan now. `waiting`: still asking. */
export function checkoutText(
  checkout: "success" | "cancel",
  paid: boolean,
  waiting: boolean,
): { kind: "success" | "info" | "warning"; text: string } {
  if (checkout === "cancel") return { kind: "info", text: "You stopped the checkout. Nothing changed." };
  if (paid) return { kind: "success", text: "Stripe confirmed the payment. The new plan is active." };
  if (waiting) return { kind: "info", text: "Waiting for Stripe to confirm the payment…" };
  return {
    kind: "warning",
    text: "Stripe did not confirm the payment yet. Reload the page in a minute.",
  };
}

/** The text of a subscription status of Stripe. */
export function subscriptionStatus(
  status: string,
  cancelAtPeriodEnd: boolean,
): string {
  if (cancelAtPeriodEnd && (status === "active" || status === "trialing")) {
    return "Ends at the end of the period";
  }
  switch (status) {
    case "active":
      return "Active";
    case "trialing":
      return "Trial";
    case "past_due":
      return "Payment failed: Stripe tries the card again";
    case "canceled":
      return "Cancelled";
    case "incomplete":
    case "incomplete_expired":
      return "Payment not complete";
    case "unpaid":
      return "Unpaid";
    case "paused":
      return "Paused";
    default:
      return status;
  }
}

/** True for the answer of the API when a model quota of the plan is used up (HTTP 402). */
export function isQuotaError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 402;
}

/** Only an http(s) URL from the API is opened, so a bad answer cannot run script. */
export function safeRedirect(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:"
      ? parsed.toString()
      : null;
  } catch {
    return null;
  }
}
