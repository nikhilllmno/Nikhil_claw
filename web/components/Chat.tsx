"use client";

// One chat session: history, live stream, approvals, questions, and the composer.

import Link from "next/link";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  type Approval,
  errorText,
  type ChatMessage,
  type ClarifyRequest,
  type ExportFormat,
  type PlanOut,
  OPEN_RUN_STATUSES,
  type OpenRun,
  type SessionDetail,
  type SteerNote,
} from "@/lib/api";
import {
  applyStreamEvent,
  BG_USAGE,
  type Effect,
  hasPendingNote,
  initialLive,
  isPaused,
  type LiveState,
  checkpointListText,
  HANDOFF_USAGE,
  JOINED_NOTICE,
  noteLabel,
  openRunIds,
  parseCommand,
  QUEUED_NOTICE,
  runNumbers,
  ROLLBACK_USAGE,
  sessionTitle,
  STEER_USAGE,
  STEERED_NOTICE,
  STOP_USAGE,
} from "@/lib/chat";
import { isQuotaError } from "@/lib/billing";
import {
  NO_PROPOSED_PLAN,
  PLAN_REJECTED_NOTICE,
  planCommandError,
  planModeNotice,
  planStatusText,
  proposedPlan,
} from "@/lib/coding";
import { saveFile } from "@/lib/history";
import { runEventStream } from "@/lib/stream";
import { runHasEnded } from "@/lib/subagentProgress";
import { ApprovalCard } from "./ApprovalCard";
import { useApi } from "./AuthProvider";
import { Composer } from "./Composer";
import { useData } from "./DataProvider";
import { Markdown } from "./Markdown";
import { QuestionCard } from "./QuestionCard";
import { ToolGroup } from "./ToolGroup";
import { ConnectCard } from "./ConnectCard";
import { cardsOfCalls } from "@/lib/connectCard";
import { ErrorState, LoadingState } from "./ui";
import styles from "./Chat.module.css";

const QUESTION_POLL_MS = 4_000;
/** How often the chat looks again while a message waits to join its run (M7.1). */
const NOTE_POLL_MS = 2_000;

export function Chat({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const { me, setWorkspaceId, reloadSessions } = useData();
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[] | null>(null);
  const [messagesError, setMessagesError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [approvalsError, setApprovalsError] = useState<string | null>(null);
  const [questions, setQuestions] = useState<ClarifyRequest[]>([]);
  // The messages that joined a running run (busy mode `steer`, M7.1).
  const [notes, setNotes] = useState<SteerNote[]>([]);
  const [live, setLive] = useState<LiveState>(initialLive);
  const liveRef = useRef<LiveState>(initialLive);
  const [connection, setConnection] = useState<{ connected: boolean; error: string | null }>({
    connected: false,
    error: null,
  });
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  /** The last send failed on a model quota of the plan (HTTP 402). */
  const [quotaError, setQuotaError] = useState(false);
  // The answer of a busy control: queued, steered, or moved (M3.9).
  const [notice, setNotice] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  // The newest plan of the session, for the plan card (M7.9). `null`: none, or not loaded.
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [tick, setTick] = useState(0);
  const localId = useRef(0);
  // Keep the view at the end while new text arrives, unless the user scrolled up.
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  const setLiveState = useCallback((next: LiveState) => {
    liveRef.current = next;
    setLive(next);
  }, []);

  const loadMessages = useCallback(async () => {
    try {
      setMessages(await api.listMessages(sessionId));
      setMessagesError(null);
    } catch (e) {
      setMessagesError(errorText(e, "Could not load the messages."));
    }
  }, [api, sessionId]);
  const loadApprovals = useCallback(async () => {
    try {
      setApprovals(await api.listApprovals(sessionId));
      setApprovalsError(null);
    } catch (e) {
      // The cards that are shown stay: they are still valid. The user can try again.
      setApprovalsError(errorText(e, "Could not load the approvals."));
    }
  }, [api, sessionId]);
  const loadNotes = useCallback(async () => {
    try {
      setNotes(await api.listSteers(sessionId));
    } catch {
      // The notes that are shown stay. The next poll or reload tries again.
    }
  }, [api, sessionId]);
  const loadQuestions = useCallback(async () => {
    try {
      setQuestions(await api.listQuestions(sessionId));
    } catch {
      // Same as for approvals.
    }
  }, [api, sessionId]);

  const loadPlan = useCallback(async () => {
    try {
      setPlan((await api.getCoding(sessionId)).plan);
    } catch {
      // The plan card is a help. The Code page shows the error.
    }
  }, [api, sessionId]);

  const run = useCallback(
    (effects: Effect[]) => {
      for (const effect of new Set(effects)) {
        if (effect === "reload_messages") {
          void loadMessages();
          void loadNotes();
          void loadPlan();
          reloadSessions();
        } else if (effect === "reload_approvals") void loadApprovals();
        else void loadQuestions();
      }
    },
    [loadMessages, loadNotes, loadPlan, loadApprovals, loadQuestions, reloadSessions],
  );

  // Load the session, then follow its event stream.
  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      let loaded: SessionDetail;
      try {
        loaded = await api.getSession(sessionId);
      } catch (e) {
        if (!controller.signal.aborted) setLoadError(errorText(e, "Could not load the session."));
        return;
      }
      if (controller.signal.aborted) return;
      setDetail(loaded);
      setWorkspaceId(loaded.workspace_id);
      const runs = Object.fromEntries(
        loaded.runs
          .filter((r) => OPEN_RUN_STATUSES.has(r.status))
          .map((r) => [
            r.id,
            { run_id: r.id, status: r.status, failure_code: null, failure_message: null },
          ]),
      );
      setLiveState({ ...initialLive, runs });
      void loadMessages();
      void loadNotes();
      void loadPlan();
      void loadApprovals();
      void loadQuestions();
      let connectedBefore = false;
      await runEventStream({
        api,
        sessionId,
        after: loaded.last_event_id,
        signal: controller.signal,
        onConnection: (connected, error) => {
          setConnection({ connected, error });
          // A run may have paused while the stream was away.
          if (connected && connectedBefore) void loadApprovals();
          connectedBefore ||= connected;
        },
        onEvent: (name, data) => {
          const result = applyStreamEvent(liveRef.current, name, data);
          if (result.state !== liveRef.current) setLiveState(result.state);
          run(result.effects);
        },
      });
    })();
    return () => controller.abort();
    // `tick` restarts the load after an error.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, sessionId, tick]);

  const openRuns = openRunIds(live);
  const running = openRuns.length > 0;
  const paused = isPaused(live);
  const stoppingNow = stopping && running;

  // The clarify tool has no stream event for the question, so look while a run is open.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void loadQuestions(), QUESTION_POLL_MS);
    return () => clearInterval(timer);
  }, [running, loadQuestions]);

  // A message that joined a run is pending until the run reads it at its next step. Look while a
  // run works too: another tab or person can add a message to the run (M7.1).
  const notePending = hasPendingNote(notes);
  useEffect(() => {
    if (!notePending && !running) return;
    const timer = setInterval(() => void loadNotes(), NOTE_POLL_MS);
    return () => clearInterval(timer);
  }, [notePending, running, loadNotes]);

  // The numbers of the open runs come from the server, so `/stop NUMBER` and the list agree.
  const [serverRuns, setServerRuns] = useState<OpenRun[] | null>(null);
  const [runsTry, setRunsTry] = useState(0);
  const openKey = [...openRuns].sort().join(",");
  useEffect(() => {
    if (openRuns.length < 2) return;
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    api
      .listOpenRuns(sessionId)
      .then((runs) => {
        if (live) setServerRuns(runs);
      })
      .catch(() => {
        // Try again: the numbers stay hidden until a list matches the open runs.
        if (live) retry = setTimeout(() => setRunsTry((n) => n + 1), NOTE_POLL_MS);
      });
    return () => {
      live = false;
      if (retry !== undefined) clearTimeout(retry);
    };
    // `openKey` stands for the open runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, sessionId, openKey, runsTry]);

  // The queued, steered, and joined notices are about the open runs: hide them when no run is open.
  const runNotices = [QUEUED_NOTICE, STEERED_NOTICE, JOINED_NOTICE];
  const shownNotice = notice !== null && (running || !runNotices.includes(notice)) ? notice : null;

  const send = async (parallel = false) => {
    const submitted = draft;
    const text = submitted.trim();
    if (!text) return;
    // Only the text that was sent goes: what the user typed meanwhile stays.
    const clearDraft = () => setDraft((current) => (current === submitted ? "" : current));
    const command = parseCommand(text);
    setSendError(null);
    setQuotaError(false);
    setNotice(null);
    const planProblem = command?.kind === "plan" ? planCommandError(command.plan) : null;
    if (planProblem !== null) {
      setSendError(planProblem);
      return;
    }
    if (command?.kind === "stop") {
      if (!command.valid) {
        setSendError(STOP_USAGE);
        return;
      }
      clearDraft();
      if (!running) setNotice("No run is open now.");
      else if (command.number === null) await stop();
      else await stopNumber(command.number);
      return;
    }
    if (command?.kind === "bg" && !command.task) {
      setSendError(BG_USAGE);
      return;
    }
    if (command?.kind === "steer" && !command.note) {
      setSendError(STEER_USAGE);
      return;
    }
    if (command?.kind === "handoff" && command.to === null) {
      setSendError(HANDOFF_USAGE);
      return;
    }
    if (command?.kind === "rollback" && !command.valid) {
      setSendError(ROLLBACK_USAGE);
      return;
    }
    setSending(true);
    setStopping(false);
    try {
      if (command?.kind === "handoff" && command.to !== null) {
        const moved = await api.handoff(sessionId, command.to);
        clearDraft();
        setNotice(moved.text);
        reloadSessions();
        return;
      }
      if (command?.kind === "coding") {
        // The direct mode of the session (M7.2).
        const answer = await api.codingAgent(sessionId, command.agent);
        clearDraft();
        setNotice(answer.text);
        return;
      }
      if (command?.kind === "undo" || command?.kind === "rollback") {
        // The checkpoints of the session files (M6.4).
        let text: string;
        if (command.kind === "undo") text = (await api.undo(sessionId)).text;
        else if (command.number === null) text = checkpointListText(await api.listCheckpoints(sessionId));
        else text = (await api.rollback(sessionId, command.number)).text;
        clearDraft();
        setNotice(text);
        return;
      }
      if (command?.kind === "plan" && ["status", "mode"].includes(command.plan.kind)) {
        // Plan mode and the newest plan (M7.9).
        let notice: string;
        if (command.plan.kind === "mode") {
          await api.setPlanMode(sessionId, command.plan.on);
          notice = planModeNotice(command.plan.on);
        } else {
          notice = planStatusText(await api.getCoding(sessionId));
        }
        clearDraft();
        setNotice(notice);
        void loadPlan();
        return;
      }
      let runId: string;
      let queued = false;
      let userText: string | null = text;
      if (command?.kind === "plan") {
        const step = command.plan;
        if (step.kind === "start") {
          ({ run_id: runId, queued } = await api.startPlan(sessionId, step.task));
          userText = step.task;
        } else {
          // Approve, edit, or reject the newest plan that waits for a decision.
          const waiting = proposedPlan((await api.getCoding(sessionId)).plan);
          if (waiting === null) {
            clearDraft();
            setPlan(null);
            setNotice(NO_PROPOSED_PLAN);
            return;
          }
          userText = null;
          if (step.kind === "reject") {
            const answer = await api.rejectPlan(sessionId, waiting.id, step.feedback || undefined);
            clearDraft();
            if (answer.run_id === null) {
              setNotice(PLAN_REJECTED_NOTICE);
              return;
            }
            ({ run_id: runId, queued } = { run_id: answer.run_id, queued: answer.queued });
          } else {
            const body = step.kind === "edit" ? step.text : undefined;
            ({ run_id: runId, queued } = await api.approvePlan(sessionId, waiting.id, body));
          }
        }
      } else if (command?.kind === "compact") {
        ({ run_id: runId } = await api.compact(sessionId, command.focus));
        userText = null;
      } else if (command?.kind === "steer") {
        const answer = await api.steer(sessionId, command.note);
        if (answer.steered) {
          clearDraft();
          setNotice(STEERED_NOTICE);
          void loadNotes();
          return;
        }
        // No run was open: the API sent the note as a message.
        ({ run_id: runId, queued } = answer);
        userText = command.note;
      } else if (command?.kind === "bg") {
        // `/bg TASK` starts a run at the same time as the open runs (M7.1).
        ({ run_id: runId, queued } = await api.sendMessage(sessionId, command.task, true));
        userText = command.task;
      } else {
        const answer = await api.sendMessage(sessionId, text, parallel);
        if (answer.steered) {
          // The message joined the running run (busy mode `steer`, M7.1). It shows as a note.
          clearDraft();
          setNotice(JOINED_NOTICE);
          const now = new Date().toISOString();
          localId.current += 1;
          const note: SteerNote = {
            id: `local-${localId.current}`,
            run_id: answer.run_id,
            text,
            status: "pending",
            created_at: now,
            delivered_at: null,
            requeued_run_id: null,
          };
          setNotes((current) => [...current, note]);
          stick.current = true;
          void loadNotes();
          return;
        }
        ({ run_id: runId, queued } = answer);
      }
      clearDraft();
      if (queued) setNotice(QUEUED_NOTICE);
      if (userText !== null) {
        const now = new Date().toISOString();
        localId.current += 1;
        const message: ChatMessage = {
          id: `local-${localId.current}`,
          seq: Number.MAX_SAFE_INTEGER,
          run_id: runId,
          role: "user",
          author_user_id: null,
          text: userText,
          created_at: now,
        };
        setMessages((current) => [...(current ?? []), message]);
      }
      const current = liveRef.current;
      if (current.runs[runId] === undefined) {
        setLiveState({
          ...current,
          failure: null,
          runs: {
            ...current.runs,
            [runId]: { run_id: runId, status: "queued", failure_code: null, failure_message: null },
          },
        });
      }
      stick.current = true;
      void loadMessages();
      reloadSessions();
    } catch (e) {
      const message = errorText(e, "The message was not sent.");
      // The text stays in the composer: nothing was sent. When the queue refuses the run of
      // a plan decision, the API undoes the decision, so the plan stays open.
      setSendError(message);
      setQuotaError(isQuotaError(e));
    } finally {
      // A plan command can change the plan: show its state of now.
      if (command?.kind === "plan") void loadPlan();
      setSending(false);
    }
  };

  const stop = async () => {
    setStopping(true);
    setSendError(null);
    setQuotaError(false);
    const results = await Promise.allSettled(openRunIds(liveRef.current).map((id) => api.cancelRun(id)));
    const failed = results.find((r) => r.status === "rejected");
    if (failed && failed.status === "rejected") {
      setSendError(errorText(failed.reason, "The run was not stopped."));
      setStopping(false);
    }
  };

  /** `/stop NUMBER`: stops the run at that place of the open runs, oldest first (M7.1). */
  const stopNumber = async (number: number) => {
    setSendError(null);
    try {
      const runs = await api.listOpenRuns(sessionId);
      const chosen = runs.find((r) => r.number === number);
      if (!chosen) {
        setSendError(`There is no open run ${number}. ${STOP_USAGE}`);
        return;
      }
      await api.cancelRun(chosen.run_id);
      setNotice(`Stopping run ${number}.`);
    } catch (e) {
      setSendError(errorText(e, "The run was not stopped."));
    }
  };

  const stopOne = async (runId: string) => {
    setSendError(null);
    try {
      await api.cancelRun(runId);
    } catch (e) {
      setSendError(errorText(e, "The run was not stopped."));
    }
  };

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const timeline = useMemo(() => {
    const items: React.ReactNode[] = [];
    const shown = new Set<string>();
    // The notes that joined a run go between the messages, by time.
    const pending = notes.filter((n) => noteLabel(n) !== null);
    let next = 0;
    const notesBefore = (time: string | null) => {
      for (let note = pending[next]; note && (time === null || note.created_at < time); note = pending[next]) {
        items.push(<NoteView key={`note-${note.id}`} note={note} />);
        next += 1;
      }
    };
    const progress = (runId: string) => {
      const calls = live.tools[runId] ?? [];
      const subagents = live.subagents[runId] ?? [];
      if (calls.length === 0 && subagents.length === 0) return null;
      // The Connect cards of `connection_request` (M8.1) go under the tool calls of the run.
      const cards = cardsOfCalls(calls);
      return (
        <Fragment key={`tools-${runId}`}>
          <ToolGroup calls={calls} subagents={subagents} runEnded={runHasEnded(live.runs[runId]?.status)} />
          {cards.map((card, index) => (
            <ConnectCard key={`connect-${runId}-${card.connectionId || index}`} card={card} />
          ))}
        </Fragment>
      );
    };
    for (const m of messages ?? []) {
      notesBefore(m.created_at);
      if (m.role === "assistant" && m.run_id && !shown.has(m.run_id)) {
        shown.add(m.run_id);
        const group = progress(m.run_id);
        if (group) items.push(group);
      }
      items.push(<MessageView key={m.id} message={m} />);
    }
    notesBefore(null);
    for (const runId of new Set([...Object.keys(live.tools), ...Object.keys(live.subagents)])) {
      if (shown.has(runId)) continue;
      const group = progress(runId);
      if (group) items.push(group);
    }
    return items;
  }, [messages, notes, live.tools, live.subagents, live.runs]);

  // The open runs, oldest request first, numbered as `/stop NUMBER` counts them (M7.1).
  const runRows = useMemo(() => {
    const requests = new Map<string, ChatMessage>();
    for (const m of messages ?? []) {
      if (m.role === "user" && m.run_id && !requests.has(m.run_id)) requests.set(m.run_id, m);
    }
    const order = runNumbers(openRuns, serverRuns);
    return openRuns
      .map((id) => ({
        id,
        status: live.runs[id]?.status ?? "queued",
        text: requests.get(id)?.text ?? order.get(id)?.text ?? null,
        number: order.get(id)?.number ?? null,
        seq: requests.get(id)?.seq ?? Number.MAX_SAFE_INTEGER,
      }))
      .sort((a, b) => (a.number ?? Number.MAX_SAFE_INTEGER) - (b.number ?? Number.MAX_SAFE_INTEGER) || a.seq - b.seq);
  }, [openRuns, live.runs, messages, serverRuns]);
  // A run that the server list does not have yet shows no number: `/stop NUMBER` uses the server list.
  const runNumber = new Map(
    runRows.filter((row) => row.number !== null).map((row) => [row.id, row.number as number]),
  );

  if (loadError) {
    return (
      <ErrorState
        title="This session did not load"
        message={loadError}
        onRetry={() => {
          setLoadError(null);
          setTick((n) => n + 1);
        }}
      />
    );
  }
  if (detail === null) return <LoadingState label="Loading the session" />;

  const org = me?.orgs.find((o) => o.id === detail.org_id) ?? null;
  const profile = org?.agent_profiles.find((p) => p.id === detail.agent_profile_id) ?? null;
  const liveText = Object.entries(live.text).filter(([, text]) => text !== "");
  const working = running && !paused && liveText.length === 0;

  return (
    <div className={styles.chat}>
      <header className={styles.header}>
        <div className={styles.headerText}>
          <h1 className={styles.title} title={sessionTitle(detail)}>
            {sessionTitle(detail)}
          </h1>
          <p className={styles.meta}>
            {profile ? (
              <Link href={`/agents/${profile.id}`}>{profile.name}</Link>
            ) : (
              "Agent"
            )}
            {" · "}
            {detail.audience === "shared" ? "Shared with the workspace" : "Only you"}
          </p>
        </div>
        <div className={styles.headerActions}>
          {paused ? (
            <span className="badge badge-warning">Waiting for you</span>
          ) : running ? (
            <span className="badge badge-accent">
              <span className="spinner" aria-hidden="true" />{" "}
              {openRuns.length > 1 ? `${openRuns.length} runs working` : "Working"}
            </span>
          ) : null}
          <SessionTools sessionId={sessionId} />
        </div>
      </header>

      <div
        ref={scroller}
        className={styles.scroll}
        onScroll={(event) => {
          const el = event.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <div className={styles.stream} aria-live="polite" aria-relevant="additions">
          {messages === null && !messagesError ? (
            <div aria-busy="true" className={styles.skeletons}>
              <div className="skeleton" style={{ height: "var(--space-7)", width: "60%" }} />
              <div className="skeleton" style={{ height: "var(--space-8)" }} />
              <div className="skeleton" style={{ height: "var(--space-7)", width: "75%" }} />
            </div>
          ) : null}
          {messagesError ? (
            <div className="alert alert-error" role="alert">
              {messagesError}{" "}
              <button type="button" className="btn btn-small" onClick={() => void loadMessages()}>
                Try again
              </button>
            </div>
          ) : null}
          {messages !== null && messages.length === 0 && !running ? (
            <div className="state">
              <h2>No messages yet</h2>
              <p>Say what you want done. The agent answers here as it works.</p>
            </div>
          ) : null}
          {timeline}
          {liveText.map(([runId, text]) => (
            <article
              key={`live-${runId}`}
              className={styles.assistant}
              aria-label={runRows.length > 1 ? `Assistant, writing in run ${runNumber.get(runId) ?? ""}` : "Assistant, writing"}
            >
              {runRows.length > 1 && runNumber.has(runId) ? (
                <p className={styles.runLabel}>Run {runNumber.get(runId)}</p>
              ) : null}
              <Markdown text={text} />
              <span className={styles.caret} aria-hidden="true" />
            </article>
          ))}
          {Object.entries(live.stopped).map(([runId, text]) => (
            <article key={`stopped-${runId}`} className={styles.assistant} aria-label="Assistant, stopped">
              {text ? <Markdown text={text} /> : null}
              <p className={styles.stoppedNote}>Stopped. This partial reply is not saved.</p>
            </article>
          ))}
          {working ? (
            <p className={styles.working}>
              <span className="spinner" aria-hidden="true" /> Working on it…
            </p>
          ) : null}
          {live.failure ? (
            <p className="alert alert-error" role="alert">
              {live.failure}
            </p>
          ) : null}
        </div>
      </div>

      <div className={styles.dock}>
        {connection.error ? (
          <p className={`alert alert-warning ${styles.banner}`} role="status">
            Live updates paused: {connection.error} Trying again.
          </p>
        ) : null}
        {approvalsError ? (
          <p className={`alert alert-error ${styles.banner}`} role="alert">
            {approvalsError}{" "}
            <button type="button" className="btn btn-small" onClick={() => void loadApprovals()}>
              Try again
            </button>
          </p>
        ) : null}
        {approvals.map((approval) => (
          <ApprovalCard
            key={approval.id}
            approval={approval}
            onDecided={() => {
              setApprovals((list) => list.filter((a) => a.id !== approval.id));
              void loadApprovals();
            }}
          />
        ))}
        {questions.map((request) => (
          <QuestionCard
            key={request.id}
            sessionId={sessionId}
            request={request}
            onAnswered={() => {
              setQuestions((list) => list.filter((r) => r.id !== request.id));
              void loadQuestions();
            }}
          />
        ))}
        {proposedPlan(plan) !== null ? (
          <p className={`alert alert-warning ${styles.banner}`} role="status">
            The agent proposed plan {plan?.version}. Open the <Link href={`/s/${sessionId}/code`}>Code page</Link>{" "}
            to approve, edit, or reject it. Or send /plan approve.
          </p>
        ) : null}
        {sendError ? (
          <p className={`alert alert-error ${styles.banner}`} role="alert">
            {sendError}
            {quotaError ? (
              <>
                {" "}
                <Link href="/billing">Open the Billing page</Link>
              </>
            ) : null}
          </p>
        ) : null}
        {shownNotice ? (
          <p className={`alert ${styles.banner}`} role="status">
            {shownNotice}
          </p>
        ) : null}
        {runRows.length > 1 ? (
          <section className={styles.runs} aria-label="Runs that work at the same time">
            <h2 className={styles.runsTitle}>{runRows.length} runs at the same time</h2>
            <ol className={styles.runList}>
              {runRows.map((row) => (
                <li key={row.id} className={styles.runRow}>
                  <span className={styles.runNumber}>{row.number === null ? "Run" : `Run ${row.number}`}</span>
                  <span className={styles.runText} title={row.text ?? undefined}>
                    {row.text ?? "Waiting for the request"}
                  </span>
                  <span className={`badge ${row.status === "paused" ? "badge-warning" : "badge-accent"}`}>
                    {row.status === "paused" ? "Waiting for you" : row.status === "queued" ? "Waiting" : "Working"}
                  </span>
                  <button
                    type="button"
                    className="btn btn-quiet btn-small"
                    aria-label={row.number === null ? "Stop this run" : `Stop run ${row.number}`}
                    onClick={() => void stopOne(row.id)}
                  >
                    Stop
                  </button>
                </li>
              ))}
            </ol>
          </section>
        ) : null}
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={() => void send()}
          onSendParallel={() => void send(true)}
          sending={sending}
          running={running}
          stopping={stoppingNow}
          onStop={() => void stop()}
          stopLabel={openRuns.length > 1 ? "Stop all" : "Stop"}
        />
      </div>
    </div>
  );
}

/** A message that joined a running run (busy mode `steer`, M7.1), with its state. */
function NoteView({ note }: { note: SteerNote }) {
  return (
    <article className={styles.user} aria-label="You, added to the current run">
      <p className={styles.userText}>{note.text}</p>
      <p className={styles.noteStatus}>{noteLabel(note)}</p>
    </article>
  );
}

function MessageView({ message: m }: { message: ChatMessage }) {
  if (m.role === "user") {
    return (
      <article className={styles.user} aria-label="You">
        <p className={styles.userText}>{m.text}</p>
      </article>
    );
  }
  if (m.role === "assistant") {
    return (
      <article className={styles.assistant} aria-label="Assistant">
        <Markdown text={m.text} />
      </article>
    );
  }
  return (
    <p className={styles.note}>
      {m.role}: {m.text}
    </p>
  );
}

/** The links to the run traces and the export of the session. */
function SessionTools({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [error, setError] = useState<string | null>(null);

  const download = async (format: ExportFormat) => {
    setBusy(format);
    setError(null);
    try {
      const file = await api.exportSession(sessionId, format);
      saveFile(file.name, file.blob);
    } catch (e) {
      setError(errorText(e, "The export failed."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={styles.tools}>
      <Link href={`/s/${sessionId}/code`} className="btn btn-quiet btn-small">
        Code
      </Link>
      <Link href={`/s/${sessionId}/runs`} className="btn btn-quiet btn-small">
        Runs
      </Link>
      <button
        type="button"
        className="btn btn-quiet btn-small"
        disabled={busy !== null}
        aria-label="Export .md: the session as Markdown"
        onClick={() => void download("markdown")}
      >
        {busy === "markdown" ? "Exporting" : "Export .md"}
      </button>
      <button
        type="button"
        className="btn btn-quiet btn-small"
        disabled={busy !== null}
        aria-label="Export .json: the session as JSON"
        onClick={() => void download("json")}
      >
        {busy === "json" ? "Exporting" : "Export .json"}
      </button>
      {error ? (
        <span className={styles.toolsError} role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
