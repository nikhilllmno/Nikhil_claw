"use client";

// One chat session: history, live stream, approvals, questions, and the composer.

import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  type Approval,
  errorText,
  type ChatMessage,
  type ClarifyRequest,
  type ExportFormat,
  OPEN_RUN_STATUSES,
  type SessionDetail,
} from "@/lib/api";
import {
  applyStreamEvent,
  type Effect,
  initialLive,
  isPaused,
  type LiveState,
  checkpointListText,
  HANDOFF_USAGE,
  openRunIds,
  parseCommand,
  QUEUED_NOTICE,
  ROLLBACK_USAGE,
  sessionTitle,
  STEER_USAGE,
  STEERED_NOTICE,
} from "@/lib/chat";
import { saveFile } from "@/lib/history";
import { runEventStream } from "@/lib/stream";
import { ApprovalCard } from "./ApprovalCard";
import { useApi } from "./AuthProvider";
import { Composer } from "./Composer";
import { useData } from "./DataProvider";
import { Markdown } from "./Markdown";
import { QuestionCard } from "./QuestionCard";
import { ToolGroup } from "./ToolGroup";
import { ErrorState, LoadingState } from "./ui";
import styles from "./Chat.module.css";

const QUESTION_POLL_MS = 4_000;

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
  const [live, setLive] = useState<LiveState>(initialLive);
  const liveRef = useRef<LiveState>(initialLive);
  const [connection, setConnection] = useState<{ connected: boolean; error: string | null }>({
    connected: false,
    error: null,
  });
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  // The answer of a busy control: queued, steered, or moved (M3.9).
  const [notice, setNotice] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
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
  const loadQuestions = useCallback(async () => {
    try {
      setQuestions(await api.listQuestions(sessionId));
    } catch {
      // Same as for approvals.
    }
  }, [api, sessionId]);

  const run = useCallback(
    (effects: Effect[]) => {
      for (const effect of new Set(effects)) {
        if (effect === "reload_messages") {
          void loadMessages();
          reloadSessions();
        } else if (effect === "reload_approvals") void loadApprovals();
        else void loadQuestions();
      }
    },
    [loadMessages, loadApprovals, loadQuestions, reloadSessions],
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

  // The queued and steered notices are about the open runs: hide them when no run is open.
  const shownNotice =
    notice !== null && (running || (notice !== QUEUED_NOTICE && notice !== STEERED_NOTICE)) ? notice : null;

  const send = async () => {
    const submitted = draft;
    const text = submitted.trim();
    if (!text) return;
    // Only the text that was sent goes: what the user typed meanwhile stays.
    const clearDraft = () => setDraft((current) => (current === submitted ? "" : current));
    const command = parseCommand(text);
    setSendError(null);
    setNotice(null);
    if (command?.kind === "stop") {
      clearDraft();
      if (running) await stop();
      else setNotice("No run is open now.");
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
      let runId: string;
      let queued = false;
      let userText: string | null = text;
      if (command?.kind === "compact") {
        ({ run_id: runId } = await api.compact(sessionId, command.focus));
        userText = null;
      } else if (command?.kind === "steer") {
        const answer = await api.steer(sessionId, command.note);
        if (answer.steered) {
          clearDraft();
          setNotice(STEERED_NOTICE);
          return;
        }
        // No run was open: the API sent the note as a message.
        ({ run_id: runId, queued } = answer);
        userText = command.note;
      } else {
        ({ run_id: runId, queued } = await api.sendMessage(sessionId, text));
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
      // The text stays in the composer: nothing was sent.
      setSendError(errorText(e, "The message was not sent."));
    } finally {
      setSending(false);
    }
  };

  const stop = async () => {
    setStopping(true);
    setSendError(null);
    const results = await Promise.allSettled(openRunIds(liveRef.current).map((id) => api.cancelRun(id)));
    const failed = results.find((r) => r.status === "rejected");
    if (failed && failed.status === "rejected") {
      setSendError(errorText(failed.reason, "The run was not stopped."));
      setStopping(false);
    }
  };

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const timeline = useMemo(() => {
    const items: React.ReactNode[] = [];
    const shown = new Set<string>();
    for (const m of messages ?? []) {
      if (m.role === "assistant" && m.run_id && !shown.has(m.run_id)) {
        shown.add(m.run_id);
        const calls = live.tools[m.run_id];
        if (calls) items.push(<ToolGroup key={`tools-${m.run_id}`} calls={calls} />);
      }
      items.push(<MessageView key={m.id} message={m} />);
    }
    for (const [runId, calls] of Object.entries(live.tools)) {
      if (!shown.has(runId)) items.push(<ToolGroup key={`tools-${runId}`} calls={calls} />);
    }
    return items;
  }, [messages, live.tools]);

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
              <span className="spinner" aria-hidden="true" /> Working
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
            <article key={`live-${runId}`} className={styles.assistant} aria-label="Assistant, writing">
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
        {sendError ? (
          <p className={`alert alert-error ${styles.banner}`} role="alert">
            {sendError}
          </p>
        ) : null}
        {shownNotice ? (
          <p className={`alert ${styles.banner}`} role="status">
            {shownNotice}
          </p>
        ) : null}
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={() => void send()}
          sending={sending}
          running={running}
          stopping={stoppingNow}
          onStop={() => void stop()}
        />
      </div>
    </div>
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
