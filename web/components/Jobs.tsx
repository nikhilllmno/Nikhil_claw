"use client";

// The Jobs screen: the scheduled jobs of the org, a form to make or change one, and the detail of
// one job with its recent fires and delivery receipts.

import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { type AgentProfileSummary, type Job, type JobFire, type SchedulePreview, errorText } from "@/lib/api";
import {
  type JobAction,
  type JobDraft,
  CURATOR_TEXT,
  approvalNotice,
  availableActions,
  browserTimeZone,
  buildInput,
  creatorLabel,
  deliveryStatusLabel,
  deliveryStatusTone,
  failureNotice,
  fireStatusLabel,
  fireStatusTone,
  formatTarget,
  formatWhen,
  isCurator,
  modeLabel,
  newDraft,
  sortJobs,
  statusLabel,
  statusTone,
  toDraft,
  validateDraft,
} from "@/lib/jobs";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { ErrorState, LoadingState, ToneBadge } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Jobs.module.css";

type View = { kind: "list" } | { kind: "new" } | { kind: "detail"; id: string } | { kind: "edit"; job: Job };

/**
 * True inside the settings console (M7.10): the screen has no page frame and its titles are `h2`,
 * because the section of the console has the `h1`.
 */
const EmbeddedContext = createContext(false);

/** The class of the frame of a view: the page, or a plain column inside the console. */
function useFrame() {
  return useContext(EmbeddedContext) ? styles.embedded : styles.page;
}

/** The title of a view. It is an `h2` inside the console. */
function Title({ children, ...props }: React.ComponentPropsWithRef<"h1">) {
  return useContext(EmbeddedContext) ? (
    <h2 {...props} className={styles.title}>
      {children}
    </h2>
  ) : (
    <h1 {...props} className={styles.title}>
      {children}
    </h1>
  );
}

/** Puts the focus on a heading when a view opens, so that a screen reader reads the new view. */
function useHeadingFocus() {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    ref.current?.focus();
  }, []);
  return ref;
}

export function Jobs({ embedded = false, canCreate = true }: { embedded?: boolean; canCreate?: boolean } = {}) {
  const { me, meError, reloadMe, workspaceId, orgOfWorkspace } = useData();
  const org = orgOfWorkspace(workspaceId);

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading your jobs" />;
  if (org === null || workspaceId === null) {
    return (
      <ErrorState
        title="No workspace"
        message="You are not in a workspace yet. Ask an admin of your org to add you."
      />
    );
  }
  return (
    <EmbeddedContext.Provider value={embedded}>
      <JobsScreen key={org.id} orgId={org.id} workspaceId={workspaceId} canCreate={canCreate} />
    </EmbeddedContext.Provider>
  );
}

function JobsScreen({
  orgId,
  workspaceId,
  canCreate,
}: {
  orgId: string;
  workspaceId: string;
  canCreate: boolean;
}) {
  const api = useApi();
  const { profilesFor } = useData();
  const [view, setView] = useState<View>({ kind: "list" });
  const loadJobs = useMemo(() => () => api.listJobs(orgId), [api, orgId]);
  const { data: jobs, error, reload } = useLoad(loadJobs, "Could not load the jobs.");

  if (view.kind === "new") {
    return (
      <JobForm
        key="new"
        orgId={orgId}
        workspaceId={workspaceId}
        job={null}
        profiles={profilesFor(workspaceId)}
        onSaved={(saved) => {
          reload();
          setView({ kind: "detail", id: saved.id });
        }}
        onCancel={() => setView({ kind: "list" })}
      />
    );
  }
  if (view.kind === "edit") {
    return (
      <JobForm
        key={view.job.id}
        orgId={orgId}
        workspaceId={view.job.workspace_id}
        job={view.job}
        profiles={profilesFor(view.job.workspace_id)}
        onSaved={(saved) => {
          reload();
          setView({ kind: "detail", id: saved.id });
        }}
        onCancel={() => setView({ kind: "detail", id: view.job.id })}
      />
    );
  }
  if (view.kind === "detail") {
    return (
      <JobDetail
        key={view.id}
        orgId={orgId}
        jobId={view.id}
        onBack={() => {
          reload();
          setView({ kind: "list" });
        }}
        onChanged={reload}
        onEdit={(job) => setView({ kind: "edit", job })}
      />
    );
  }

  return (
    <JobList
      jobs={jobs}
      error={error}
      onRetry={reload}
      onNew={canCreate ? () => setView({ kind: "new" }) : null}
      onOpen={(job) => setView({ kind: "detail", id: job.id })}
    />
  );
}

// -- the list -----------------------------------------------------------------------------------

function JobList({
  jobs,
  error,
  onRetry,
  onNew,
  onOpen,
}: {
  jobs: Job[] | null;
  error: string | null;
  onRetry: () => void;
  /** `null`: the user cannot make jobs, so the list has no New job button. */
  onNew: (() => void) | null;
  onOpen: (job: Job) => void;
}) {
  const headingRef = useHeadingFocus();
  const frame = useFrame();
  const shown = useMemo(() => sortJobs((jobs ?? []).filter((j) => j.status !== "removed")), [jobs]);
  const waiting = shown.filter((j) => j.status === "pending_approval").length;

  return (
    <div className={frame}>
      <header className={styles.header}>
        <div className={styles.headerRow}>
          <Title ref={headingRef} tabIndex={-1}>
            Jobs
          </Title>
          {onNew ? (
            <button type="button" className="btn btn-primary" onClick={onNew}>
              New job
            </button>
          ) : null}
        </div>
        <p className={styles.meta}>A job runs the agent, or a script, on a schedule and sends the result.</p>
        {onNew ? null : <p className={styles.meta}>Your role cannot make jobs. An org admin can change this.</p>}
      </header>

      {error && jobs === null ? (
        <ErrorState title="The jobs did not load" message={error} onRetry={onRetry} />
      ) : jobs === null ? (
        <LoadingState label="Loading your jobs" />
      ) : shown.length === 0 ? (
        <div className="state">
          <h2>No jobs yet</h2>
          <p>
            {onNew
              ? "Make a job to run the agent on a schedule, for example every weekday at 9am."
              : "Nobody made a job yet."}
          </p>
          {onNew ? (
            <button type="button" className="btn btn-primary" onClick={onNew}>
              New job
            </button>
          ) : null}
        </div>
      ) : (
        <>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}{" "}
              <button type="button" className="btn btn-small" onClick={onRetry}>
                Try again
              </button>
            </p>
          ) : null}
          {waiting > 0 ? (
            <p className="alert alert-warning" role="note">
              {waiting === 1 ? "1 job needs" : `${waiting} jobs need`} your approval before it runs.
            </p>
          ) : null}
          <ul className={styles.list}>
            {shown.map((job) => (
              <li key={job.id} className={`card ${styles.job}`}>
                <div className={styles.row}>
                  <button type="button" className={styles.jobName} onClick={() => onOpen(job)}>
                    {job.name}
                  </button>
                  <ToneBadge tone={statusTone(job.status)}>{statusLabel(job.status)}</ToneBadge>
                </div>
                <dl className={styles.details}>
                  <dt>Schedule</dt>
                  <dd>{job.schedule_text}</dd>
                  <dt>Next run</dt>
                  <dd>{job.next_fire_at ? formatWhen(job.next_fire_at, job.schedule.tz) : "None"}</dd>
                </dl>
                {job.status === "paused" && job.paused_reason ? (
                  <p className="hint">Paused: {job.paused_reason}</p>
                ) : null}
                {job.consecutive_failures > 0 ? (
                  <p className="hint">{failureNotice(job.consecutive_failures)}</p>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

// -- the detail ---------------------------------------------------------------------------------

const ACTION_TEXT: Record<JobAction, { label: string; busy: string; done: string }> = {
  run: { label: "Run now", busy: "Starting…", done: "The job started. Its fire shows below when it begins." },
  pause: { label: "Pause", busy: "Pausing…", done: "The job is paused." },
  resume: { label: "Resume", busy: "Resuming…", done: "The job is active." },
  cancel: { label: "Cancel runs", busy: "Cancelling…", done: "" },
  remove: { label: "Remove", busy: "Removing…", done: "" },
};

function JobDetail({
  orgId,
  jobId,
  onBack,
  onChanged,
  onEdit,
}: {
  orgId: string;
  jobId: string;
  onBack: () => void;
  onChanged: () => void;
  onEdit: (job: Job) => void;
}) {
  const api = useApi();
  const headingRef = useHeadingFocus();
  const frame = useFrame();
  const load = useMemo(() => () => api.getJob(orgId, jobId), [api, orgId, jobId]);
  const { data: job, setData: setJob, error: loadError, reload } = useLoad(load, "Could not load the job.");
  // The name of the action that runs, or "approve".
  const [busy, setBusy] = useState<JobAction | "approve" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);

  if (loadError && job === null) {
    return (
      <div className={frame}>
        <button type="button" className={styles.back} onClick={onBack}>
          Back to jobs
        </button>
        <ErrorState title="The job did not load" message={loadError} onRetry={reload} />
      </div>
    );
  }
  if (job === null) return <LoadingState label="Loading the job" />;

  const attempt = async (name: JobAction | "approve", call: () => Promise<void>) => {
    setBusy(name);
    setError(null);
    setNotice(null);
    try {
      await call();
    } catch (e) {
      setError(errorText(e, "The action failed."));
    } finally {
      setBusy(null);
    }
  };

  const perform = (action: JobAction) =>
    attempt(action, async () => {
      setConfirmRemove(false);
      if (action === "remove") {
        await api.deleteJob(orgId, job.id);
        onBack();
        return;
      }
      if (action === "run") await api.runJob(orgId, job.id);
      else if (action === "pause") await api.pauseJob(orgId, job.id);
      else if (action === "resume") await api.resumeJob(orgId, job.id);
      else {
        const { cancelled } = await api.cancelJob(orgId, job.id);
        setNotice(
          cancelled === 0
            ? "No run was open."
            : cancelled === 1
              ? "1 run is cancelled."
              : `${cancelled} runs are cancelled.`,
        );
      }
      if (ACTION_TEXT[action].done) setNotice(ACTION_TEXT[action].done);
      onChanged();
      // The answer of pause and resume has no fires, so read the job again.
      reload();
    });

  const approve = () =>
    attempt("approve", async () => {
      const approved = await api.approveJob(orgId, job.id, job.version);
      setJob({ ...approved, fires: job.fires });
      setNotice(`Version ${approved.version} is approved.`);
      onChanged();
      reload();
    });

  const actions = availableActions(job.status);
  const approval = job.status === "pending_approval" ? approvalNotice(job) : null;
  const failures = failureNotice(job.consecutive_failures);
  const tz = job.schedule.tz;

  return (
    <div className={frame}>
      <header className={styles.header}>
        <button type="button" className={styles.back} onClick={onBack}>
          Back to jobs
        </button>
        <div className={styles.headerRow}>
          <Title ref={headingRef} tabIndex={-1}>
            {job.name}
          </Title>
          <ToneBadge tone={statusTone(job.status)}>{statusLabel(job.status)}</ToneBadge>
        </div>
        <p className={styles.meta}>
          Version {job.version} · made by {creatorLabel(job.created_by_kind)}
        </p>
      </header>

      {approval ? (
        <section className={`alert alert-warning ${styles.section}`} aria-labelledby="needs-approval">
          <h2 className={styles.heading} id="needs-approval">
            Needs your approval
          </h2>
          <p>{approval.summary}</p>
          {approval.reasons.length > 0 ? (
            <ul className={styles.reasons}>
              {approval.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          ) : null}
          <div className={styles.actions}>
            <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={approve}>
              {busy === "approve" ? "Approving…" : `Approve version ${job.version}`}
            </button>
            {isCurator(job) ? null : (
              <button type="button" className="btn" disabled={busy !== null} onClick={() => onEdit(job)}>
                Change the job
              </button>
            )}
          </div>
        </section>
      ) : null}

      {job.status === "paused" && job.paused_reason ? (
        <p className="alert" role="note">
          Paused: {job.paused_reason}
        </p>
      ) : null}
      {failures ? (
        <p className="alert alert-error" role="note">
          {failures}
        </p>
      ) : null}
      <div aria-live="polite">
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : notice ? (
          <p className="alert alert-success">{notice}</p>
        ) : null}
      </div>
      {loadError ? (
        <p className="alert alert-error" role="alert">
          {loadError}{" "}
          <button type="button" className="btn btn-small" onClick={reload}>
            Try again
          </button>
        </p>
      ) : null}

      <div className={styles.actions}>
        {isCurator(job) ? null : (
          <button type="button" className="btn" disabled={busy !== null} onClick={() => onEdit(job)}>
            Edit
          </button>
        )}
        {actions.map((action) =>
          action === "remove" ? null : (
            <button
              key={action}
              type="button"
              className="btn"
              disabled={busy !== null}
              onClick={() => perform(action)}
            >
              {busy === action ? ACTION_TEXT[action].busy : ACTION_TEXT[action].label}
            </button>
          ),
        )}
        {actions.includes("remove") ? (
          confirmRemove ? (
            <>
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy !== null}
                onClick={() => perform("remove")}
              >
                {busy === "remove" ? ACTION_TEXT.remove.busy : "Remove the job"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy !== null}
                onClick={() => setConfirmRemove(false)}
              >
                Keep the job
              </button>
            </>
          ) : (
            <button
              type="button"
              className="btn btn-danger"
              disabled={busy !== null}
              onClick={() => setConfirmRemove(true)}
            >
              Remove
            </button>
          )
        ) : null}
      </div>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>Schedule</h2>
        <dl className={styles.details}>
          <dt>Runs</dt>
          <dd>{job.schedule_text}</dd>
          <dt>Next runs</dt>
          <dd>
            {job.next_fires.length === 0 ? (
              "None"
            ) : (
              <ul className={styles.times}>
                {job.next_fires.map((when) => (
                  <li key={when}>{formatWhen(when, tz)}</li>
                ))}
              </ul>
            )}
          </dd>
          <dt>Last run</dt>
          <dd>{job.last_fire_at ? formatWhen(job.last_fire_at, tz) : "Never"}</dd>
        </dl>
      </section>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>{isCurator(job) ? "What it does" : job.mode === "script" ? "Script" : "Prompt"}</h2>
        {isCurator(job) ? (
          <p>{CURATOR_TEXT}</p>
        ) : (
          <pre className={styles.pre}>{job.mode === "script" ? job.script : job.prompt}</pre>
        )}
        <dl className={styles.details}>
          <dt>Mode</dt>
          <dd>{modeLabel(job.mode)}</dd>
          <dt>Send to</dt>
          <dd>{formatTarget(job.deliver)}</dd>
          <dt>On failure</dt>
          <dd>{job.failure_deliver ? formatTarget(job.failure_deliver) : "Same as send to"}</dd>
          <dt>Model</dt>
          <dd>{job.model ?? "Default"}</dd>
          <dt>Skills</dt>
          <dd>{job.skills.length > 0 ? job.skills.join(", ") : "None"}</dd>
          <dt>Chat</dt>
          <dd>
            {job.attach_to_session ? "Runs in one long chat session" : "Runs in a new session each time"}
          </dd>
        </dl>
        {job.preapproved_tools.length > 0 ? (
          <>
            <h3 className="label">Pre-approved tools</h3>
            <pre className={styles.pre}>{JSON.stringify(job.preapproved_tools, null, 2)}</pre>
          </>
        ) : null}
      </section>

      <section className={`card ${styles.section}`}>
        <div className={styles.row}>
          <h2 className={styles.heading}>Recent runs</h2>
          <button type="button" className="btn btn-small" onClick={reload}>
            Refresh
          </button>
        </div>
        {job.fires.length === 0 ? (
          <p className="hint">This job has not run yet.</p>
        ) : (
          <ul className={styles.fires}>
            {job.fires.map((fire) => (
              <FireItem key={fire.id} fire={fire} tz={tz} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function FireItem({ fire, tz }: { fire: JobFire; tz: string | undefined }) {
  return (
    <li className={styles.fire}>
      <div className={styles.row}>
        <span>
          {formatWhen(fire.scheduled_for, tz)}
          {fire.manual ? " · started by you" : ""}
        </span>
        <ToneBadge tone={fireStatusTone(fire.status)}>{fireStatusLabel(fire.status)}</ToneBadge>
      </div>
      {fire.error ? (
        <p className="alert alert-error" role="note">
          {fire.error}
        </p>
      ) : null}
      {fire.output ? <pre className={styles.pre}>{fire.output}</pre> : null}
      {fire.deliveries.length > 0 ? (
        <ul className={styles.receipts} aria-label="Delivery receipts">
          {fire.deliveries.map((delivery, index) => (
            <li key={`${delivery.target}-${index}`} className={styles.receipt}>
              <ToneBadge tone={deliveryStatusTone(delivery.status)}>{deliveryStatusLabel(delivery.status)}</ToneBadge>
              <span>{formatTarget(delivery.target)}</span>
              {delivery.error ? <span className={styles.errorText}>{delivery.error}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

// -- the form -----------------------------------------------------------------------------------

/** The IANA zones that the browser knows. Empty when it cannot list them. */
function timeZoneNames(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

/** The check of a schedule text by the API, after the user stops typing. */
function useSchedulePreview(schedule: string, tz: string, enabled: boolean) {
  const api = useApi();
  const text = schedule.trim();
  const zone = tz.trim();
  const key = `${zone}\n${text}`;
  const active = enabled && text !== "";
  const [state, setState] = useState<{
    key: string;
    preview: SchedulePreview | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api.previewSchedule(text, zone || undefined, controller.signal).then(
        (preview) => setState({ key, preview, error: null }),
        (e: unknown) => {
          if (!controller.signal.aborted) {
            setState({ key, preview: null, error: errorText(e, "Could not check the schedule.") });
          }
        },
      );
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, active, text, zone, key]);

  const current = active && state?.key === key ? state : null;
  return {
    checking: active && current === null,
    preview: current?.preview ?? null,
    error: current?.error ?? null,
  };
}

function JobForm({
  orgId,
  workspaceId,
  job,
  profiles,
  onSaved,
  onCancel,
}: {
  orgId: string;
  workspaceId: string;
  /** The job to change, or `null` for a new job. */
  job: Job | null;
  profiles: AgentProfileSummary[];
  onSaved: (job: Job) => void;
  onCancel: () => void;
}) {
  const api = useApi();
  const headingRef = useHeadingFocus();
  const frame = useFrame();
  const initial = useMemo<JobDraft | null>(() => (job === null ? null : toDraft(job)), [job]);
  // The draft when the form opened. A new form starts with the time zone of the browser.
  const [baseline] = useState<JobDraft>(() => initial ?? newDraft(browserTimeZone(), profiles[0]?.id ?? ""));
  const [draft, setDraft] = useState<JobDraft>(baseline);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const zones = useMemo(() => timeZoneNames(), []);

  const edit = (change: Partial<JobDraft>) => setDraft((d) => ({ ...d, ...change }));
  const input = useMemo(() => buildInput(draft, initial, { workspaceId }), [draft, initial, workspaceId]);
  // A new job can be saved at once: the form has its first values. An update needs a change.
  const canSave = job === null || Object.keys(input).length > 0;
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const problem = validateDraft(draft);

  // The preview runs for a new job and for a schedule or zone that the user changed.
  const scheduleChanged =
    initial === null ||
    draft.schedule.trim() !== initial.schedule.trim() ||
    draft.tz.trim() !== initial.tz.trim();
  const {
    checking,
    preview,
    error: previewError,
  } = useSchedulePreview(draft.schedule, draft.tz, scheduleChanged);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSave || problem || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved =
        job === null ? await api.createJob(orgId, input) : await api.updateJob(orgId, job.id, input);
      onSaved(saved);
    } catch (e) {
      setError(errorText(e, "The job was not saved."));
      setSaving(false);
    }
  };

  const tz = draft.tz.trim() || undefined;
  return (
    <form className={frame} onSubmit={save}>
      <header className={styles.header}>
        <button type="button" className={styles.back} onClick={onCancel}>
          {job === null ? "Back to jobs" : "Back to the job"}
        </button>
        <Title ref={headingRef} tabIndex={-1}>
          {job === null ? "New job" : `Edit ${job.name}`}
        </Title>
        {job !== null && job.status === "pending_approval" ? (
          <p className="alert alert-warning" role="note">
            This job waits for your approval. Your change saves a new version, and you approve it.
          </p>
        ) : null}
      </header>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>General</h2>
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={draft.name}
            maxLength={200}
            placeholder="Morning digest"
            onChange={(e) => edit({ name: e.target.value })}
          />
        </label>

        <div className={styles.pair}>
          <label className="field">
            <span className="label">Schedule</span>
            <input
              className="input"
              value={draft.schedule}
              aria-describedby="schedule-preview"
              placeholder="every weekday at 9am"
              onChange={(e) => edit({ schedule: e.target.value })}
            />
            <span className="hint">
              For example: every 2 hours, daily at 9am, in 30 minutes, or a cron line such as 0 9 * * 1-5.
            </span>
          </label>
          <label className="field">
            <span className="label">Time zone</span>
            <input
              className="input"
              list="job-time-zones"
              value={draft.tz}
              maxLength={64}
              onChange={(e) => edit({ tz: e.target.value })}
            />
            <datalist id="job-time-zones">
              {zones.map((zone) => (
                <option key={zone} value={zone} />
              ))}
            </datalist>
            <span className="hint">An IANA name, for example Asia/Kolkata.</span>
          </label>
        </div>

        <div className={styles.preview} id="schedule-preview" aria-live="polite">
          <span className="label">Preview</span>
          {!scheduleChanged && job !== null ? (
            <>
              <span className={styles.previewTitle}>{job.schedule_text}</span>
              <NextTimes times={job.next_fires} tz={job.schedule.tz} />
            </>
          ) : draft.schedule.trim() === "" ? (
            <span className="hint">Write a schedule to see when the job runs.</span>
          ) : checking ? (
            <span className="hint">Checking the schedule…</span>
          ) : previewError ? (
            <span className={styles.errorText} role="alert">
              {previewError}
            </span>
          ) : preview ? (
            <>
              <span className={styles.previewTitle}>{preview.text}</span>
              <NextTimes times={preview.next_fires} tz={tz} />
            </>
          ) : null}
        </div>

        <label className="field">
          <span className="label">Prompt</span>
          <textarea
            className={`input ${styles.prompt}`}
            rows={6}
            value={draft.prompt}
            placeholder="Tell the agent what to do each time the job runs."
            onChange={(e) => edit({ prompt: e.target.value })}
          />
          {draft.mode === "script" ? (
            <span className="hint">A script job does not use the prompt.</span>
          ) : null}
        </label>
      </section>

      <section className={`card ${styles.section}`}>
        <h2 className={styles.heading}>Result</h2>
        <div className={styles.pair}>
          <label className="field">
            <span className="label">Send the result to</span>
            <input
              className="input"
              value={draft.deliver}
              placeholder="origin"
              onChange={(e) => edit({ deliver: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">Send a failure to</span>
            <input
              className="input"
              value={draft.failureDeliver}
              placeholder="Same as the result"
              onChange={(e) => edit({ failureDeliver: e.target.value })}
            />
          </label>
        </div>
        <p className="hint">
          Use origin, none, slack:#channel, telegram:&lt;chat id&gt;, or email:&lt;address&gt;. A job that
          sends to another place than your own chat needs an approval when an agent makes it.
        </p>
        <label className="field">
          <span className="label">Model</span>
          <input
            className="input"
            value={draft.model}
            placeholder="The model of the agent"
            onChange={(e) => edit({ model: e.target.value })}
          />
        </label>
        {profiles.length > 1 ? (
          <label className="field">
            <span className="label">Agent</span>
            <select
              className="input"
              value={draft.agentProfileId}
              onChange={(e) => edit({ agentProfileId: e.target.value })}
            >
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className={styles.check}>
          <input
            type="checkbox"
            checked={draft.attachToSession}
            onChange={(e) => edit({ attachToSession: e.target.checked })}
          />
          <span>
            Run in one long session
            <span className="hint">Each run goes in the same session, not in a new one.</span>
          </span>
        </label>
      </section>

      <details
        className={`card ${styles.advanced}`}
        open={draft.mode === "script" || draft.preapprovedTools !== "" || draft.skills !== ""}
      >
        <summary>Advanced</summary>
        <div className={styles.advancedBody}>
          <label className="field">
            <span className="label">Mode</span>
            <select
              className="input"
              value={draft.mode}
              onChange={(e) => edit({ mode: e.target.value === "script" ? "script" : "agent" })}
            >
              <option value="agent">Agent: the agent follows the prompt</option>
              <option value="script">Script: a script runs in the sandbox</option>
            </select>
            {draft.mode === "script" ? (
              <span className="hint">A script job needs your approval when an agent makes it.</span>
            ) : null}
          </label>
          {draft.mode === "script" ? (
            <label className="field">
              <span className="label">Script</span>
              <textarea
                className={`input ${styles.prompt}`}
                rows={8}
                value={draft.script}
                onChange={(e) => edit({ script: e.target.value })}
              />
            </label>
          ) : null}
          <label className="field">
            <span className="label">Skills</span>
            <input
              className="input"
              value={draft.skills}
              placeholder="Names, separated by commas"
              onChange={(e) => edit({ skills: e.target.value })}
            />
          </label>
          <label className="field">
            <span className="label">Pre-approved tools</span>
            <textarea
              className={`input ${styles.json}`}
              rows={6}
              value={draft.preapprovedTools}
              disabled={draft.mode === "script"}
              placeholder={'[{"tool": "web_fetch", "limits": {"host": ["example.com"]}}]'}
              onChange={(e) => edit({ preapprovedTools: e.target.value })}
            />
            <span className="hint">
              A JSON list. Each item has a tool and optional limits. The job can use these tools without a
              question. Leave it empty for none.
            </span>
          </label>
        </div>
      </details>

      <div className={styles.bar}>
        <div className={styles.barText} aria-live="polite">
          {error ? (
            <span className={styles.errorText} role="alert">
              {error}
            </span>
          ) : problem && dirty ? (
            <span className={styles.errorText}>{problem}</span>
          ) : dirty && job !== null ? (
            <span>Unsaved changes</span>
          ) : null}
        </div>
        <button type="button" className="btn" disabled={saving} onClick={onCancel}>
          Cancel
        </button>
        <button type="submit" className="btn btn-primary" disabled={!canSave || saving || problem !== null}>
          {saving ? "Saving…" : job === null ? "Make the job" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function NextTimes({ times, tz }: { times: string[]; tz: string | undefined }) {
  if (times.length === 0) return <span className="hint">The schedule has no next run.</span>;
  return (
    <>
      <span className="hint">Next runs</span>
      <ul className={styles.times}>
        {times.map((when) => (
          <li key={when}>{formatWhen(when, tz)}</li>
        ))}
      </ul>
    </>
  );
}
