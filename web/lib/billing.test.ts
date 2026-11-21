import { describe, expect, it } from "vitest";
import { ApiError, type BillingOverview, type BillingPlan } from "./api";
import {
  checkoutText,
  isQuotaError,
  planAction,
  safeRedirect,
  stripeReturn,
  subscriptionStatus,
  usd,
  usedPercent,
} from "./billing";

function plan(name: string, paid: boolean): BillingPlan {
  return {
    name,
    title: name,
    price_usd: paid ? "20.00" : "0.00",
    included_usd: "0.00",
    paid,
    max_members: 5,
    model_usd_per_month: "5.00",
    model_usd_per_user_day: "2.00",
    max_running_runs: 2,
    run_budget: {},
  };
}

function overview(
  current: string,
  change: Partial<BillingOverview> = {},
): BillingOverview {
  return {
    org_id: "o1",
    billing_enabled: true,
    plan: plan(current, current !== "free"),
    subscription: null,
    usage: {
      period_start: null,
      period_end: null,
      model_usd: "0.00",
      included_usd: null,
      billable_usd: null,
      reported_usd: null,
    },
    quotas: {
      model_usd_per_month: "5.00",
      model_usd_this_month: "0.00",
      model_usd_per_user_day: "2.00",
      max_members: 5,
      members: 1,
      max_running_runs: 2,
      user_messages_per_minute: 10,
      org_messages_per_minute: 30,
    },
    can_buy: false,
    can_change: false,
    can_open_portal: false,
    ...change,
  };
}

describe("usd", () => {
  it("shows two decimals and a fallback", () => {
    expect(usd("20")).toBe("USD 20.00");
    expect(usd("3.5")).toBe("USD 3.50");
    expect(usd(null)).toBe("No limit");
    expect(usd(undefined, "-")).toBe("-");
    expect(usd("x", "-")).toBe("-");
  });
});

describe("usedPercent", () => {
  it("is null without a quota and stays from 0 to 100", () => {
    expect(usedPercent("1", null)).toBeNull();
    expect(usedPercent("2.5", "5")).toBe(50);
    expect(usedPercent("9", "5")).toBe(100);
    expect(usedPercent(null, "5")).toBe(0);
    expect(usedPercent("0", "0")).toBe(100);
  });
});

describe("planAction", () => {
  const pro = plan("pro", true);
  it("marks the current plan", () => {
    expect(planAction(overview("pro", { can_change: true }), pro)).toBe(
      "current",
    );
  });
  it("offers to buy for a free org and to change for a paying org", () => {
    expect(planAction(overview("free", { can_buy: true }), pro)).toBe("buy");
    expect(planAction(overview("business", { can_change: true }), pro)).toBe(
      "change",
    );
  });
  it("offers nothing without billing or for the free plan", () => {
    expect(planAction(overview("free"), pro)).toBe("none");
    expect(
      planAction(overview("pro", { can_change: true }), plan("free", false)),
    ).toBe("none");
  });
});

describe("stripeReturn", () => {
  const org = "3f2b8c1e-1d2a-4b5c-8d9e-0a1b2c3d4e5f";
  it("reads the org and the checkout result", () => {
    expect(stripeReturn(`?org=${org}&checkout=success`)).toEqual({ org, checkout: "success" });
    expect(stripeReturn(`?org=${org}`)).toEqual({ org, checkout: null });
    expect(stripeReturn("?checkout=cancel")).toEqual({ org: null, checkout: "cancel" });
  });
  it("drops values that are not valid", () => {
    expect(stripeReturn("?org=x&checkout=other")).toEqual({ org: null, checkout: null });
    expect(stripeReturn("")).toEqual({ org: null, checkout: null });
  });
});

describe("checkoutText", () => {
  it("does not claim a payment before the plan changes", () => {
    expect(checkoutText("success", false, true).text).toContain("Waiting");
    expect(checkoutText("success", false, false).kind).toBe("warning");
    expect(checkoutText("success", true, false).kind).toBe("success");
    expect(checkoutText("cancel", false, false).text).toContain("Nothing changed");
  });
});

describe("subscriptionStatus", () => {
  it("names the statuses of Stripe", () => {
    expect(subscriptionStatus("active", false)).toBe("Active");
    expect(subscriptionStatus("active", true)).toBe(
      "Ends at the end of the period",
    );
    expect(subscriptionStatus("past_due", false)).toContain("Payment failed");
    expect(subscriptionStatus("strange", false)).toBe("strange");
  });
});

describe("isQuotaError", () => {
  it("is true only for HTTP 402", () => {
    expect(isQuotaError(new ApiError(402, "quota"))).toBe(true);
    expect(isQuotaError(new ApiError(429, "slow down"))).toBe(false);
    expect(isQuotaError(new Error("x"))).toBe(false);
  });
});

describe("safeRedirect", () => {
  it("opens only http(s) URLs", () => {
    expect(safeRedirect("https://checkout.stripe.com/c/pay/cs_test_1")).toBe(
      "https://checkout.stripe.com/c/pay/cs_test_1",
    );
    expect(safeRedirect("javascript:alert(1)")).toBeNull();
    expect(safeRedirect("not a url")).toBeNull();
  });
});
