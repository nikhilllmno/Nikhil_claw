"use client";

// The Billing page (docs/architecture.md, Section 17.5). An org owner or admin sees the plan,
// the model usage, and the quotas. With billing on, the admin buys a plan with Stripe Checkout,
// changes the plan, or opens the Stripe customer portal (payment method, invoices, cancel).

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { type BillingOverview, type BillingPlan, errorText, type Org } from "@/lib/api";
import {
  CONFIRM_ATTEMPTS,
  CONFIRM_INTERVAL_MS,
  checkoutText,
  planAction,
  safeRedirect,
  stripeReturn,
  subscriptionStatus,
  usd,
  usedPercent,
} from "@/lib/billing";
import { isAdmin } from "@/lib/orgs";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";
import own from "./Billing.module.css";

const noSubscribe = () => () => {};

type Checkout = "success" | "cancel" | null;

export function Billing() {
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  const [chosen, setChosen] = useState<string | null>(null);
  // The return from Stripe names the org, so the admin comes back to the same org.
  const search = useSyncExternalStore(noSubscribe, () => window.location.search, () => "");
  const returned = useMemo(() => stripeReturn(search), [search]);
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the billing" />;
  const admins = me.orgs.filter((o) => isAdmin(o.role));
  const current = orgOfWorkspace(workspaceId);
  const org =
    admins.find((o) => o.id === (chosen ?? returned.org)) ??
    admins.find((o) => o.id === current?.id) ??
    admins[0] ??
    null;
  const checkout = org !== null && org.id === returned.org ? returned.checkout : null;

  return (
    <div className={styles.page}>
      <PageHeader title="Billing" meta="The plan of your org, the model usage, and the quotas." />
      {org === null ? (
        <p className={styles.meta}>Only an owner or an admin of an org can see its billing.</p>
      ) : (
        <>
          {admins.length > 1 ? (
            <label className="field">
              <span className="label">Org</span>
              <select className="input" value={org.id} onChange={(e) => setChosen(e.target.value)}>
                {admins.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          <OrgBilling key={org.id} org={org} checkout={checkout} />
        </>
      )}
    </div>
  );
}

function OrgBilling({ org, checkout }: { org: Org; checkout: Checkout }) {
  const api = useApi();
  const loadOverview = useCallback(() => api.billingOverview(org.id), [api, org.id]);
  const overview = useLoad(loadOverview, "Could not load the billing.");
  const loadPlans = useCallback(() => api.billingPlans(), [api]);
  const plans = useLoad(loadPlans, "Could not load the plans.");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // After Checkout, ask again until the Stripe webhook changed the plan, a bounded number of
  // times. The page does not claim a payment from the return address alone.
  const [attempts, setAttempts] = useState(0);
  const paid = overview.data?.plan.paid ?? false;
  const waiting = checkout === "success" && !paid && attempts < CONFIRM_ATTEMPTS;
  const loaded = overview.data !== null;
  const { reload } = overview;
  useEffect(() => {
    if (!waiting || !loaded) return;
    const timer = window.setTimeout(() => {
      setAttempts((n) => n + 1);
      reload();
    }, CONFIRM_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [waiting, loaded, attempts, reload]);
  const notice = checkout === null ? null : checkoutText(checkout, paid, waiting);
  const locked = waiting ? "waiting" : busy;

  const redirect = async (key: string, call: () => Promise<{ url: string }>) => {
    setBusy(key);
    setError(null);
    try {
      const { url } = await call();
      const target = safeRedirect(url);
      if (target === null) throw new Error("Stripe sent an address that is not valid.");
      window.location.assign(target);
    } catch (e) {
      setError(errorText(e, "Stripe did not open. Try again."));
      setBusy(null);
    }
  };

  const change = async (plan: BillingPlan) => {
    if (!window.confirm(`Change the plan to ${plan.title}? Stripe prorates the price.`)) return;
    setBusy(plan.name);
    setError(null);
    try {
      overview.setData(await api.changePlan(org.id, plan.name));
    } catch (e) {
      setError(errorText(e, "The plan did not change."));
    } finally {
      setBusy(null);
    }
  };

  if (overview.error) return <ErrorState message={overview.error} onRetry={overview.reload} />;
  const data = overview.data;
  if (data === null) return <LoadingState label="Loading the billing" />;

  return (
    <>
      {notice ? (
        <p className={`alert ${NOTICE_CLASS[notice.kind]}`} role="status">
          {notice.text}
        </p>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <PlanCard
        org={org}
        data={data}
        busy={locked}
        onPortal={() => void redirect("portal", () => api.openBillingPortal(org.id))}
      />
      <UsageCard data={data} />
      <section className={`card ${styles.card}`} aria-labelledby="billing-plans">
        <div className={styles.cardHead}>
          <h2 id="billing-plans" className={styles.heading}>
            Plans
          </h2>
          {!data.billing_enabled ? (
            <span className={styles.meta}>Billing is not set up on this server.</span>
          ) : null}
        </div>
        {plans.error ? (
          <div className={styles.row}>
            <p className={styles.error}>{plans.error}</p>
            <button type="button" className="btn btn-small" onClick={plans.reload}>
              Try again
            </button>
          </div>
        ) : plans.data === null ? (
          <p className={styles.meta}>Loading…</p>
        ) : (
          <ul className={own.plans}>
            {plans.data.plans.map((plan) => (
              <PlanOption
                key={plan.name}
                plan={plan}
                action={planAction(data, plan)}
                busy={locked}
                onBuy={() => void redirect(plan.name, () => api.startCheckout(org.id, plan.name))}
                onChange={() => void change(plan)}
              />
            ))}
          </ul>
        )}
      </section>
    </>
  );
}

const NOTICE_CLASS = { success: "alert-success", warning: "alert-warning", info: "" } as const;

function PlanCard({
  org,
  data,
  busy,
  onPortal,
}: {
  org: Org;
  data: BillingOverview;
  busy: string | null;
  onPortal: () => void;
}) {
  const sub = data.subscription;
  const price =
    data.plan.price_usd === null ? "Not for sale" : `${usd(data.plan.price_usd)} each month`;
  return (
    <section className={`card ${styles.card}`} aria-labelledby="billing-plan">
      <div className={styles.cardHead}>
        <h2 id="billing-plan" className={styles.heading}>
          {org.name}
        </h2>
        <span className="badge badge-accent">{data.plan.title || data.plan.name} plan</span>
      </div>
      <dl className={own.facts}>
        <dt>Price</dt>
        <dd>{price}</dd>
        {sub && sub.has_subscription ? (
          <>
            <dt>Subscription</dt>
            <dd>{subscriptionStatus(sub.status, sub.cancel_at_period_end)}</dd>
            {sub.current_period_end ? (
              <>
                <dt>{sub.cancel_at_period_end ? "Ends" : "Renews"}</dt>
                <dd>{new Date(sub.current_period_end).toLocaleDateString()}</dd>
              </>
            ) : null}
          </>
        ) : null}
      </dl>
      {data.can_open_portal ? (
        <div className={styles.actions}>
          <button type="button" className="btn" onClick={onPortal} disabled={busy !== null}>
            {busy === "portal" ? "Opening…" : "Payment method, invoices, and cancel"}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function Meter({ label, used, cap }: { label: string; used: string | null; cap: string | null }) {
  const percent = usedPercent(used, cap);
  const level = percent ?? 0;
  const fill = level >= 100 ? own.full : level >= 80 ? own.high : own.fill;
  return (
    <div className={own.meter}>
      <div className={styles.row}>
        <span className={styles.text}>{label}</span>
        <span className={styles.meta}>
          {usd(used, "USD 0.00")} of {usd(cap)}
        </span>
      </div>
      {percent !== null ? (
        <div
          className={own.bar}
          role="progressbar"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent}
        >
          <span className={fill} style={{ width: `${percent}%` }} />
        </div>
      ) : null}
    </div>
  );
}

function UsageCard({ data }: { data: BillingOverview }) {
  const q = data.quotas;
  const u = data.usage;
  return (
    <section className={`card ${styles.card}`} aria-labelledby="billing-usage">
      <h2 id="billing-usage" className={styles.heading}>
        Usage and quotas
      </h2>
      <Meter
        label="Model cost of the org this month (UTC)"
        used={q.model_usd_this_month}
        cap={q.model_usd_per_month}
      />
      <dl className={own.facts}>
        <dt>Model quota for each user and day</dt>
        <dd>{usd(q.model_usd_per_user_day)}</dd>
        <dt>Members</dt>
        <dd>
          {q.members} of {q.max_members === null ? "no limit" : q.max_members}
        </dd>
        <dt>Runs at the same time</dt>
        <dd>{q.max_running_runs}</dd>
        {u.included_usd !== null ? (
          <>
            <dt>Model cost this billing period</dt>
            <dd>{usd(u.model_usd, "USD 0.00")}</dd>
            <dt>Included in the plan</dt>
            <dd>{usd(u.included_usd)}</dd>
            <dt>Usage over the included amount</dt>
            <dd>{usd(u.billable_usd, "USD 0.00")}</dd>
          </>
        ) : null}
      </dl>
      <p className={styles.meta}>
        When the org uses its monthly quota, new messages stop until the next month (UTC). When a
        user uses the daily quota, the messages of that user stop until the next day (UTC). An
        admin can change the plan to get larger quotas.
      </p>
    </section>
  );
}

function PlanOption({
  plan,
  action,
  busy,
  onBuy,
  onChange,
}: {
  plan: BillingPlan;
  action: ReturnType<typeof planAction>;
  busy: string | null;
  onBuy: () => void;
  onChange: () => void;
}) {
  const label = action === "buy" ? `Upgrade to ${plan.title}` : `Change to ${plan.title}`;
  return (
    <li className={own.plan} aria-current={action === "current" ? "true" : undefined}>
      <div className={styles.row}>
        <span className={styles.name}>
          {plan.title}
          {action === "current" ? <span className="badge">Current</span> : null}
        </span>
        <span className={styles.text}>
          {plan.paid ? `${usd(plan.price_usd)} each month` : "Free"}
        </span>
      </div>
      <ul className={own.features}>
        {plan.paid ? <li>{usd(plan.included_usd)} of model usage included, then the cost</li> : null}
        <li>Model quota: {usd(plan.model_usd_per_month)} each month</li>
        <li>
          {plan.max_members === null ? "Any number of members" : `Up to ${plan.max_members} members`}
        </li>
        <li>{plan.max_running_runs} runs at the same time</li>
      </ul>
      {action === "buy" || action === "change" ? (
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-primary"
            onClick={action === "buy" ? onBuy : onChange}
            disabled={busy !== null}
          >
            {busy === plan.name ? "Opening…" : label}
          </button>
        </div>
      ) : null}
    </li>
  );
}
