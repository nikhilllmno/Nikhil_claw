"use client";

// The setup guide after sign-up (docs/architecture.md, Section 17.6, M6.9): the org exists,
// then the user connects a chat app, picks or creates an agent, and sends a first message. The
// progress is kept in this browser for each org. Each step can be skipped or opened again.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useState } from "react";
import { errorText, type Org } from "@/lib/api";
import { usd } from "@/lib/billing";
import {
  currentStep,
  FIRST_PROMPTS,
  hasActiveChatApp,
  loadProgress,
  onboardingSteps,
  type Progress,
  saveProgress,
  type StepId,
  usableAgents,
  usableChoice,
} from "@/lib/onboarding";
import { useApi } from "./AuthProvider";
import { ChatApps } from "./ChatApps";
import { useData } from "./DataProvider";
import { NewAgentForm } from "./NewAgentForm";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import { Welcome } from "./Welcome";
import styles from "./Page.module.css";
import own from "./Admin.module.css";

export function Onboarding() {
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the setup guide" />;
  if (me.orgs.length === 0) return <Welcome />;
  const current = orgOfWorkspace(workspaceId);
  const org = current?.region_here ? current : (me.orgs.find((o) => o.region_here) ?? null);
  if (org === null) {
    return (
      <div className="state">
        <h2>Nothing to set up here</h2>
        <p>Your org is in another region. Open it on the deployment of that region.</p>
      </div>
    );
  }
  return <Guide key={org.id} org={org} />;
}

function Guide({ org }: { org: Org }) {
  const router = useRouter();
  const [progress, setProgressState] = useState<Progress>(() => loadProgress(window.localStorage, org.id));
  const steps = onboardingSteps(org, progress);
  const next = currentStep(steps);
  const [opened, setOpened] = useState<StepId | null>(null);
  const open = opened ?? next;

  const update = (change: Partial<Progress>, then: StepId | null = null) => {
    const value = { ...progress, ...change };
    saveProgress(window.localStorage, org.id, value);
    setProgressState(value);
    setOpened(then);
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link href="/" className={styles.back}>
          Back to sessions
        </Link>
        <h1 className={styles.title}>Set up {org.name}</h1>
        <p className={styles.meta}>Five short steps. You can skip a step and come back to it later.</p>
      </header>
      <ol className={own.steps}>
        {steps.map((step, index) => (
          <li
            key={step.id}
            className={`${own.step} ${step.done ? own.stepDone : ""}`}
            aria-current={open === step.id ? "step" : undefined}
          >
            <span className={own.stepMark} aria-hidden="true">
              {step.done ? "✓" : index + 1}
            </span>
            <div className={own.stepBody}>
              <div className={styles.row}>
                <span className={own.stepTitle}>
                  {step.title}
                  <span className="sr-only">{step.done ? " (done)" : " (to do)"}</span>
                </span>
                {open !== step.id && step.id !== "org" ? (
                  <button type="button" className="btn btn-quiet btn-small" onClick={() => setOpened(step.id)}>
                    {step.done ? "Open again" : "Open"}
                  </button>
                ) : null}
              </div>
              <span className="hint">{step.hint}</span>
              {open === step.id ? (
                <StepPanel
                  step={step.id}
                  org={org}
                  progress={progress}
                  onDone={(change) => update(change)}
                  onFinished={(sessionId) => {
                    update({ done: true });
                    router.push(`/s/${sessionId}`);
                  }}
                  onPickAgain={() => setOpened("agent")}
                />
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      {next === null ? (
        <section className={`card ${styles.card}`} aria-labelledby="setup-done">
          <h2 id="setup-done" className={styles.heading}>
            You are all set
          </h2>
          <p className={styles.text}>Invite your team, give the agents tools, and read the help when you need it.</p>
          <div className={styles.actions}>
            <Link href="/members" className="btn btn-primary btn-small">
              Invite your team
            </Link>
            <Link href="/connections" className="btn btn-small">
              Add tools
            </Link>
            <Link href="/help" className="btn btn-quiet btn-small">
              Read the help
            </Link>
          </div>
        </section>
      ) : (
        <div className={styles.actions}>
          <button type="button" className="btn btn-quiet btn-small" onClick={() => update({ done: true }, null)}>
            Close the guide
          </button>
        </div>
      )}
    </div>
  );
}

function StepPanel({
  step,
  org,
  progress,
  onDone,
  onFinished,
  onPickAgain,
}: {
  step: StepId;
  org: Org;
  progress: Progress;
  onDone: (change: Partial<Progress>) => void;
  onFinished: (sessionId: string) => void;
  onPickAgain: () => void;
}) {
  if (step === "plan") return <PlanStep org={org} onDone={onDone} />;
  if (step === "channel") return <ChannelStep org={org} onDone={onDone} />;
  if (step === "agent") return <AgentStep org={org} progress={progress} onDone={onDone} />;
  if (step === "message") {
    return <MessageStep org={org} progress={progress} onFinished={onFinished} onPickAgain={onPickAgain} />;
  }
  return null;
}

function PlanStep({ org, onDone }: { org: Org; onDone: (change: Partial<Progress>) => void }) {
  const api = useApi();
  const load = useCallback(() => api.billingPlans(), [api]);
  const { data, error, reload } = useLoad(load, "Could not load the plans.");
  const current = data?.plans.find((p) => p.name === org.plan) ?? null;
  return (
    <div className={styles.form}>
      {error ? (
        <p className={styles.error}>
          {error}{" "}
          <button type="button" className="btn btn-small" onClick={reload}>
            Try again
          </button>
        </p>
      ) : data === null ? (
        <LoadingState label="Loading the plans" />
      ) : (
        <>
          <ul className={own.choices}>
            {data.plans.map((plan) => (
              <li key={plan.name} className={`${own.choice} ${own.static}`} aria-current={plan.name === org.plan ? "true" : undefined}>
                <span className={own.choiceText}>
                  <strong>
                    {plan.title}
                    {plan.name === org.plan ? " (your plan)" : ""}
                  </strong>
                  <span className="hint">
                    {plan.paid ? `${usd(plan.price_usd, "Price on request")} a month` : "Free"} ·{" "}
                    {plan.max_members === null ? "No member limit" : `Up to ${plan.max_members} members`} ·{" "}
                    {`Model use: ${usd(plan.model_usd_per_month)} a month`}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          {!data.billing_enabled ? (
            <p className={styles.meta}>This deployment of sama sells no plans. Your org keeps its plan.</p>
          ) : !org.admin ? (
            <p className={styles.meta}>An owner or an admin of {org.name} changes the plan.</p>
          ) : null}
        </>
      )}
      <div className={styles.actions}>
        <button type="button" className="btn btn-primary btn-small" onClick={() => onDone({ plan: "kept" })}>
          {`Keep the ${current?.title ?? org.plan} plan`}
        </button>
        {org.admin && data?.billing_enabled ? (
          <Link href="/billing" className="btn btn-small">
            Compare and buy plans
          </Link>
        ) : null}
      </div>
    </div>
  );
}

function ChannelStep({ org, onDone }: { org: Org; onDone: (change: Partial<Progress>) => void }) {
  const { reloadMe } = useData();
  const active = hasActiveChatApp(org);
  return (
    <div className={styles.form}>
      {org.admin ? (
        <ChatApps
          org={org}
          onChanged={reloadMe}
          onConnected={() => onDone({ channel: "connected" })}
        />
      ) : (
        <p className={styles.text}>
          An admin of {org.name} connects Telegram or Slack. After that, link your chat account on the{" "}
          <Link href="/settings">Settings</Link> page.
        </p>
      )}
      <div className={styles.actions}>
        {active ? (
          <button type="button" className="btn btn-primary btn-small" onClick={() => onDone({ channel: "connected" })}>
            Next step
          </button>
        ) : (
          <button type="button" className="btn btn-small" onClick={() => onDone({ channel: "skipped" })}>
            Use only the web app for now
          </button>
        )}
      </div>
    </div>
  );
}

function AgentStep({
  org,
  progress,
  onDone,
}: {
  org: Org;
  progress: Progress;
  onDone: (change: Partial<Progress>) => void;
}) {
  const { reloadMe, workspaceId } = useData();
  const workspace = org.workspaces.find((w) => w.id === workspaceId)?.id ?? org.workspaces[0]?.id ?? null;
  // Only the agents that the workspace of the first message can use.
  const profiles = usableAgents(org.agent_profiles, workspace);
  const [picked, setChosen] = useState<string | null>(null);
  // Checked at each render, so a workspace change never keeps an agent that it cannot use.
  const chosen =
    usableChoice(profiles, workspace, picked) ??
    usableChoice(profiles, workspace, progress.agentId) ??
    profiles[0]?.id ??
    "";
  const [creating, setCreating] = useState(profiles.length === 0 && org.admin);

  if (creating) {
    return (
      <NewAgentForm
        org={org}
        workspaceId={workspace}
        onCreated={(profile) => {
          reloadMe();
          onDone({ agentId: profile.id });
        }}
        onCancel={profiles.length > 0 ? () => setCreating(false) : undefined}
      />
    );
  }

  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault();
        if (chosen) onDone({ agentId: chosen });
      }}
    >
      {profiles.length === 0 ? (
        <p className={styles.text}>The org has no agent yet. An admin creates one.</p>
      ) : (
        <fieldset className={own.choices}>
          <legend className="sr-only">Agent</legend>
          {profiles.map((p) => (
            <label key={p.id} className={own.choice}>
              <input
                type="radio"
                name="agent"
                value={p.id}
                checked={chosen === p.id}
                onChange={() => setChosen(p.id)}
              />
              <span className={own.choiceText}>
                <strong>{p.name}</strong>
                <span className="hint">
                  {p.model} · {p.workspace_id === null ? "Whole org" : "One workspace"}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
      )}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={!chosen}>
          Use this agent
        </button>
        {org.admin ? (
          <button type="button" className="btn btn-small" onClick={() => setCreating(true)}>
            Create a new agent
          </button>
        ) : null}
        {chosen ? (
          <Link href={`/agents/${chosen}`} className="btn btn-quiet btn-small">
            See its settings
          </Link>
        ) : null}
      </div>
    </form>
  );
}

function MessageStep({
  org,
  progress,
  onFinished,
  onPickAgain,
}: {
  org: Org;
  progress: Progress;
  onFinished: (sessionId: string) => void;
  onPickAgain: () => void;
}) {
  const api = useApi();
  const { workspaceId, reloadSessions } = useData();
  const workspace = org.workspaces.find((w) => w.id === workspaceId) ?? org.workspaces[0] ?? null;
  const agentId = usableChoice(org.agent_profiles, workspace?.id ?? null, progress.agentId);
  const agent = org.agent_profiles.find((p) => p.id === agentId) ?? null;
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (workspace === null) {
    return <p className={styles.text}>You need a workspace first. Ask an admin of {org.name}.</p>;
  }
  if (agent === null) {
    return (
      <div className={styles.form}>
        <p className={styles.text}>
          The workspace {workspace.name} cannot use the agent that you picked. Pick an agent again.
        </p>
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" onClick={onPickAgain}>
            Pick an agent
          </button>
        </div>
      </div>
    );
  }

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const message = text.trim();
    if (message === "") {
      setError("Type a message, or choose one of the ideas.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const session = await api.createSession({
        workspace_id: workspace.id,
        agent_profile_id: agent.id,
        audience: "private",
        title: message.length > 60 ? `${message.slice(0, 57)}…` : message,
      });
      await api.sendMessage(session.id, message);
      reloadSessions();
      onFinished(session.id);
    } catch (e) {
      setError(errorText(e, "The message was not sent."));
      setBusy(false);
    }
  };

  return (
    <form className={styles.form} onSubmit={send}>
      <p className={styles.text}>
        Sama starts a private session with <strong>{agent.name}</strong> in {workspace.name} and sends your message.
      </p>
      <div className={styles.actions} role="group" aria-label="Ideas for a first message">
        {FIRST_PROMPTS.map((prompt) => (
          <button key={prompt} type="button" className="btn btn-small" disabled={busy} onClick={() => setText(prompt)}>
            {prompt.length > 48 ? `${prompt.slice(0, 45)}…` : prompt}
          </button>
        ))}
      </div>
      <label className="field">
        <span className="label">Your message</span>
        <textarea
          className="input"
          rows={3}
          value={text}
          maxLength={32_000}
          onChange={(e) => setText(e.target.value)}
          disabled={busy}
        />
      </label>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary btn-small" disabled={busy}>
          {busy ? "Sending…" : "Send and open the chat"}
        </button>
      </div>
    </form>
  );
}
