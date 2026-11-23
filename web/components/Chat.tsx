"use client";

// One chat session: history, live stream, approvals, questions, and the composer.

import Link from "next/link";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  type Approval,
  errorText,
  imageProblem,
  type SessionModels,
  type ChatMessage,
  type CodingAgentName,
  type ClarifyRequest,
  type ExportFormat,
  type PlanOut,
  OPEN_RUN_STATUSES,
  type OpenRun,
  type SessionDetail,
  type SteerNote,
} from "@/lib/api";
import {
  applyActivity,
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
  phaseLabel,
  STARTERS,
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
import { addImages, withoutImageMarkers } from "@/lib/images";
import { elapsedText, latestTodos } from "@/lib/toolView";
import { commandItems, filesOfCalls, historyEntries, mentionItems } from "@/lib/commands";
import { runEventStream } from "@/lib/stream";
import { runHasEnded } from "@/lib/subagentProgress";
import { ApprovalCard } from "./ApprovalCard";
import { useApi } from "./AuthProvider";
import { Composer } from "./Composer";
import { useData } from "./DataProvider";
import { useNow } from "./usePolled";
import { escapeTarget } from "@/lib/approvalKeys";
import { Markdown } from "./Markdown";
import { QuestionCard } from "./QuestionCard";
import { CodePanel, type CodeTab } from "./CodePanel";
import { BrowserPanel } from "./BrowserPanel";
import { browserStep, POLL_MS as BROWSER_POLL_MS } from "@/lib/liveBrowser";
import type { BrowserTarget, LiveBrowser } from "@/lib/api";
import { PlanCard } from "./PlanPanel";
import { useMedia } from "./useMedia";
import { LiveRun, TodoList, ToolTrail } from "./ToolGroup";
import { ConnectCard } from "./ConnectCard";
import { cardsOfCalls } from "@/lib/connectCard";
import { ErrorState, LoadingState } from "./ui";
import styles from "./Chat.module.css";

const QUESTION_POLL_MS = 4_000;
/** How often the chat looks again while a message waits to join its run (M7.1). */
const NOTE_POLL_MS = 2_000;

export function Chat({ sessionId }: { sessionId: string }) {
  const api = useApi();
  const { me, setWorkspaceId, reloadSessions, sessions } = useData();
  const now = useNow();
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
  // The images of the next message (M8.2).
  const [images, setImages] = useState<File[]>([]);
  // A message that is on its way to the API: it shows at once (optimistic).
  const [pending, setPending] = useState<{ text: string; at: number } | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  /** The last send failed on a model quota of the plan (HTTP 402). */
  const [quotaError, setQuotaError] = useState(false);
  // The answer of a busy control: queued, steered, or moved (M3.9).
  const [notice, setNotice] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  // The newest plan of the session, for the plan card (M7.9). `null`: none, or not loaded.
  const [plan, setPlan] = useState<PlanOut | null>(null);
  // The code panel next to the chat (M8.2 part C): its tab, or `null` when it is closed.
  const [codeTab, setCodeTab] = useState<CodeTab | null>(null);
  const [repoCount, setRepoCount] = useState(0);
  const narrow = useMedia("(max-width: 75rem)");
  const closeCode = useCallback(() => setCodeTab(null), []);
  // The live browser panel (M8.3). It opens by itself once for each run that opens a browser.
  const [browserOpen, setBrowserOpen] = useState(false);
  const [liveBrowser, setLiveBrowser] = useState<LiveBrowser | null>(null);
  const [browserBusy, setBrowserBusy] = useState(false);
  const [browserError, setBrowserError] = useState<string | null>(null);
  const browserShownFor = useRef<string | null>(null);
  const closeBrowser = useCallback(() => setBrowserOpen(false), []);
  const [tick, setTick] = useState(0);
  const localId = useRef(0);
  // Keep the view at the end while new text arrives, unless the user scrolled up.
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // When this view first saw each open run, for the time on the working line.
  const [openedSince, setOpenedSince] = useState<Record<string, number>>({});
  const setLiveState = useCallback((next: LiveState) => {
    liveRef.current = next;
    setLive(next);
    const ids = openRunIds(next);
    setOpenedSince((before) => {
      const added = ids.filter((id) => before[id] === undefined);
      const gone = Object.keys(before).some((id) => !ids.includes(id));
      if (added.length === 0 && !gone) return before;
      const now = Date.now();
      return Object.fromEntries(ids.map((id) => [id, before[id] ?? now]));
    });
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

  // The tool calls of the runs that ended before the page opened.
  const loadActivity = useCallback(async () => {
    try {
      const activity = await api.getActivity(sessionId);
      const next = applyActivity(liveRef.current, activity.events);
      if (next !== liveRef.current) setLiveState(next);
    } catch {
      // The tool trail is a help. The messages show without it.
    }
  }, [api, sessionId, setLiveState]);

  // The skills of the workspace, for the `/` menu of the message box.
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([]);
  const workspaceOfSession = detail?.workspace_id ?? null;
  useEffect(() => {
    if (workspaceOfSession === null) return;
    let live = true;
    api
      .listSkills(workspaceOfSession)
      .then((catalog) => {
        if (live) setSkills(catalog.skills.map((s) => ({ name: s.name, description: s.description })));
      })
      .catch(() => {
        // The menu shows the commands without the skills.
      });
    return () => {
      live = false;
    };
  }, [api, workspaceOfSession]);

  // Warm the sandbox while the person types, at most once a minute (M8.2, architecture §4.4).
  const warmedAt = useRef(0);
  const warm = useCallback(() => {
    const now = Date.now();
    if (now - warmedAt.current < 60_000) return;
    warmedAt.current = now;
    api.warmSession(sessionId).catch(() => {
      // A warm is only a help: an old API has no route, and the run starts the sandbox anyway.
    });
  }, [api, sessionId]);

  // The model of the session (M8.2), as `/model` in Claude Code.
  const [models, setModels] = useState<SessionModels | null>(null);
  const [modelsError, setModelsError] = useState(false);
  const [modelBusy, setModelBusy] = useState(false);
  const loadModels = useCallback(async () => {
    setModelsError(false);
    try {
      setModels(await api.getSessionModels(sessionId));
    } catch {
      setModelsError(true);
    }
  }, [api, sessionId]);
  const changeModel = async (model: string | null) => {
    setModelBusy(true);
    setSendError(null);
    try {
      const changed = await api.setSessionModel(sessionId, model);
      setDetail((current) => (current === null ? current : { ...current, ...changed, runs: current.runs, last_event_id: current.last_event_id }));
      setModels((current) => (current === null ? current : { ...current, model: changed.model ?? current.model, is_override: changed.model_is_override ?? false }));
      setNotice(`The next run uses ${changed.model ?? model ?? "the default model"}.`);
    } catch (e) {
      setSendError(errorText(e, "The model did not change."));
    } finally {
      setModelBusy(false);
    }
  };

  // The browser of the session (M8.3): Cloud or My Chrome, and the state of the extension.
  const [chromeConnected, setChromeConnected] = useState<boolean | null>(null);
  const [chromeFailed, setChromeFailed] = useState(false);
  const [browserChoiceBusy, setBrowserChoiceBusy] = useState(false);
  const loadChrome = useCallback(async () => {
    setChromeFailed(false);
    try {
      setChromeConnected((await api.chromeState()).connected);
    } catch {
      setChromeFailed(true);
    }
  }, [api]);
  const changeBrowser = async (target: BrowserTarget) => {
    setBrowserChoiceBusy(true);
    setSendError(null);
    try {
      const changed = await api.setSessionBrowser(sessionId, target);
      setDetail((current) => (current === null ? current : { ...current, browser_target: changed.browser_target ?? target }));
      void loadChrome();
      const which =
        target === "my_chrome"
          ? "New runs in this session use your own Chrome. Keep Chrome open with the sama extension."
          : "New runs in this session use the cloud browser.";
      setNotice(
        openRunIds(liveRef.current).length > 0
          ? `${which} The run that works now keeps its browser.`
          : which,
      );
    } catch (e) {
      setSendError(errorText(e, "The browser did not change."));
    } finally {
      setBrowserChoiceBusy(false);
    }
  };

  // The direct mode of the session (M7.2): sama, Claude Code, or Codex.
  const [harnessBusy, setHarnessBusy] = useState(false);
  const changeHarness = async (agent: CodingAgentName | null) => {
    setHarnessBusy(true);
    setSendError(null);
    try {
      const answer = await api.codingAgent(sessionId, agent);
      setDetail((current) => (current === null ? current : { ...current, coding_agent: agent }));
      setNotice(answer.text);
    } catch (e) {
      setSendError(errorText(e, "The harness did not change."));
    } finally {
      setHarnessBusy(false);
    }
  };

  const loadPlan = useCallback(async () => {
    try {
      const coding = await api.getCoding(sessionId);
      setPlan(coding.plan);
      setRepoCount(coding.repos.length);
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
      void loadActivity();
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
        onEvent: (name, data, id) => {
          // The event id orders the calls of all runs (for the to-do list of the session).
          const event = name === "run_event" && id !== null && data !== null && typeof data === "object" ? { ...data, id } : data;
          const result = applyStreamEvent(liveRef.current, name, event);
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

  // The live browser of an open run (M8.3). The live view URL comes only from the API. The
  // looks run one after the other, and a change of control starts a new generation, so an old
  // answer never replaces a newer state.
  const browserGeneration = useRef(0);
  useEffect(() => {
    if (!running) return;
    let gone = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const look = async () => {
      const generation = browserGeneration.current;
      try {
        const { live: found } = await api.getLiveBrowser(sessionId);
        if (gone || generation !== browserGeneration.current) return;
        setLiveBrowser((old) => found ?? (old !== null && old.view_url ? old : null));
        if (found !== null && browserShownFor.current !== found.run_id) {
          browserShownFor.current = found.run_id;
          setCodeTab(null);
          setBrowserOpen(true);
        }
      } catch {
        // The next look tries again. The panel keeps the last state.
      } finally {
        if (!gone) timer = setTimeout(() => void look(), BROWSER_POLL_MS);
      }
    };
    void look();
    return () => {
      gone = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [running, api, sessionId]);

  const browserControl = async (action: "take" | "give_back") => {
    if (liveBrowser === null) return;
    browserGeneration.current += 1;
    setBrowserBusy(true);
    setBrowserError(null);
    try {
      await api.setBrowserControl(sessionId, liveBrowser.run_id, action);
      browserGeneration.current += 1;
      setLiveBrowser({ ...liveBrowser, control: action === "take" ? "user" : "agent" });
    } catch (e) {
      setBrowserError(errorText(e, "The control did not change."));
    } finally {
      setBrowserBusy(false);
    }
  };

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
    const files = images;
    if (!text && files.length === 0) return;
    if (files.length > 0) {
      await sendImages(text, files, parallel);
      return;
    }
    // Only the text that was sent goes: what the user typed meanwhile stays.
    let optimistic = false;
    const clearDraft = () => {
      if (!optimistic) setDraft((current) => (current === submitted ? "" : current));
    };
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
        // The harness menu shows the new mode too.
        setDetail((current) => (current === null ? current : { ...current, coding_agent: command.agent }));
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
        // Show the message at once, as Claude Code does. The draft comes back if the send fails.
        setPending({ text, at: new Date().getTime() });
        setDraft((current) => (current === submitted ? "" : current));
        // The box is clear now: what the person types while the send works must stay.
        optimistic = true;
        stick.current = true;
        let answer: Awaited<ReturnType<typeof api.sendMessage>>;
        try {
          answer = await api.sendMessage(sessionId, text, parallel);
        } catch (e) {
          setDraft((current) => (current === "" ? submitted : current));
          throw e;
        }
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
      setPending(null);
      // A plan command can change the plan: show its state of now.
      if (command?.kind === "plan") void loadPlan();
      setSending(false);
    }
  };

  /** A message with images (`POST /sessions/{id}/messages/images`). Commands do not take images. */
  const sendImages = async (text: string, files: File[], parallel: boolean) => {
    const problem = imageProblem(files);
    if (problem !== null) {
      setSendError(problem);
      return;
    }
    if (text && (parseCommand(text) !== null || /^\/(claude|codex)\s/i.test(text))) {
      setSendError("A command cannot have images. Remove the images, or write a message.");
      return;
    }
    setSending(true);
    setSendError(null);
    setQuotaError(false);
    setNotice(null);
    setPending({ text: text || `${files.length} ${files.length === 1 ? "image" : "images"}`, at: new Date().getTime() });
    stick.current = true;
    try {
      const answer = await api.sendMessageWithImages(sessionId, text, files, parallel);
      // Only the sent images go: an image that the person added while the send worked stays.
      setImages((current) => current.filter((file) => !files.includes(file)));
      setDraft((current) => (current.trim() === text ? "" : current));
      if (answer.queued) setNotice(QUEUED_NOTICE);
      const current = liveRef.current;
      if (current.runs[answer.run_id] === undefined) {
        setLiveState({
          ...current,
          failure: null,
          runs: {
            ...current.runs,
            [answer.run_id]: { run_id: answer.run_id, status: "queued", failure_code: null, failure_message: null },
          },
        });
      }
      await loadMessages();
      reloadSessions();
    } catch (e) {
      setSendError(errorText(e, "The message was not sent."));
      setQuotaError(isQuotaError(e));
    } finally {
      setPending(null);
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

  // A decision on the plan card of the chat (M7.9). The new run shows in the stream.
  const [planBusy, setPlanBusy] = useState(false);
  const decidePlan = async (call: () => Promise<string>) => {
    setPlanBusy(true);
    setSendError(null);
    try {
      const text = await call();
      // An approve starts a run, and the run shows in the chat: no note is needed.
      setNotice(/approved/i.test(text) ? null : text);
      stick.current = true;
    } catch (e) {
      setSendError(errorText(e, "The plan decision was not saved."));
    } finally {
      setPlanBusy(false);
      void loadPlan();
      void loadMessages();
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
          <ToolTrail
            calls={calls}
            subagents={subagents}
            runEnded={runHasEnded(live.runs[runId]?.status) || live.runs[runId] === undefined}
          />
          {cards.map((card, index) => (
            <ConnectCard key={`connect-${runId}-${card.connectionId || index}`} card={card} />
          ))}
        </Fragment>
      );
    };
    const open = new Set(openRunIds({ runs: live.runs }));
    for (const m of messages ?? []) {
      notesBefore(m.created_at);
      if (m.role === "assistant" && m.run_id && !shown.has(m.run_id)) {
        shown.add(m.run_id);
        const group = progress(m.run_id);
        if (group) items.push(group);
      }
      items.push(<MessageView key={m.id} message={m} sessionId={sessionId} />);
    }
    notesBefore(null);
    // Runs that ended without a reply (stopped or failed) keep their trail. Open runs show live.
    for (const runId of new Set([...Object.keys(live.tools), ...Object.keys(live.subagents)])) {
      if (shown.has(runId) || open.has(runId)) continue;
      const group = progress(runId);
      if (group) items.push(group);
    }
    return items;
  }, [messages, notes, live.tools, live.subagents, live.runs, sessionId]);

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
  const openedAt = (id: string) => openedSince[id] ?? null;

  // Esc stops the open runs, as in Claude Code and Codex. An open menu or dialog takes Esc first.
  const stopRef = useRef<() => void>(() => undefined);
  useEffect(() => {
    stopRef.current = () => {
      if (openRunIds(liveRef.current).length > 0 && !stopping) void stop();
    };
  });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      if (escapeTarget(event.target, scroller.current) !== "stop") {
        // An open menu of the chat closes first.
        const menu = document.querySelector<HTMLDetailsElement>("details[data-menu][open]");
        if (menu) {
          menu.open = false;
          event.preventDefault();
        }
        return;
      }
      stopRef.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const commandMenu = useMemo(() => commandItems(skills), [skills]);
  const mentionMenu = useMemo(
    () =>
      mentionItems(
        filesOfCalls(Object.values(live.tools).flat()),
        (sessions ?? []).filter((s) => s.id !== sessionId).slice(0, 6).map((s) => ({ id: s.id, title: sessionTitle(s) })),
      ),
    [live.tools, sessions, sessionId],
  );
  const history = useMemo(
    () => historyEntries((messages ?? []).filter((m) => m.role === "user").map((m) => m.text)),
    [messages],
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
  // The to-do list of the session (the sama `todo` tool keeps one list per session) while a run works.
  // Only the to-do calls of the open runs: a finished list of an earlier run does not show.
  const todos =
    runRows.length > 0
      ? latestTodos(runRows.flatMap((row) => live.tools[row.id] ?? []).sort((a, b) => a.order - b.order))
      : null;

  return (
    <div className={styles.layout}>
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
            <span className={`badge badge-warning ${styles.stateBadge}`}>Waiting for you</span>
          ) : running ? (
            <span className={`badge badge-accent ${styles.stateBadge}`}>
              <span className="spinner" aria-hidden="true" />{" "}
              {openRuns.length > 1 ? `${openRuns.length} runs working` : "Working"}
            </span>
          ) : null}
          {repoCount > 0 && codeTab === null ? (
            <button type="button" className={`btn btn-small ${styles.createPr}`} onClick={() => setCodeTab("prs")}>
              Create PR
            </button>
          ) : null}
          <SessionTools
            sessionId={sessionId}
            codeOpen={codeTab !== null}
            onCode={() => {
              setBrowserOpen(false);
              setCodeTab((current) => (current === null ? "changes" : null));
            }}
            browserOpen={browserOpen}
            onBrowser={() => {
              setCodeTab(null);
              setBrowserOpen((open) => !open);
            }}
          />
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
            <div className={`state ${styles.empty}`}>
              <h2>What should the agent do?</h2>
              <p>Say what you want done. The agent answers here as it works. Type / for the commands.</p>
              <div className={styles.starters}>
                {STARTERS.map((starter) => (
                  <button
                    key={starter.label}
                    type="button"
                    className="btn btn-small"
                    onClick={() => {
                      setDraft(starter.text);
                      document.getElementById("message")?.focus();
                    }}
                  >
                    {starter.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {timeline}
          {pending !== null ? (
            <>
              <article className={styles.user} aria-label="You, sending">
                <p className={styles.userText}>{pending.text}</p>
              </article>
              {runRows.length === 0 ? <WorkingLine label="Sending" since={pending.at} paused={false} /> : null}
            </>
          ) : null}
          {runRows.map((row) => (
            <LiveRun
              key={`live-${row.id}`}
              segments={live.segments[row.id] ?? []}
              calls={live.tools[row.id] ?? []}
              subagents={live.subagents[row.id] ?? []}
              afterCall={(call) =>
                cardsOfCalls([call]).map((card, index) => (
                  <ConnectCard key={`connect-${row.id}-${card.connectionId || index}`} card={card} />
                ))
              }
              label={runRows.length > 1 ? (row.number === null ? "Run" : `Run ${row.number}`) : null}
              footer={
                <WorkingLine
                  label={phaseLabel(
                    row.status,
                    live.phase[row.id],
                    (live.segments[row.id] ?? []).length > 0 || (live.tools[row.id] ?? []).length > 0,
                  )}
                  since={openedAt(row.id)}
                  paused={row.status === "paused"}
                />
              }
            />
          ))}
          {Object.entries(live.stopped).map(([runId, text]) => (
            <article key={`stopped-${runId}`} className={styles.assistant} aria-label="Assistant, stopped">
              {text ? <Markdown text={text} /> : null}
              <p className={styles.stoppedNote}>Stopped. This partial reply is not saved.</p>
            </article>
          ))}
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
        {approvals.map((approval, index) => (
          <ApprovalCard
            key={approval.id}
            active={index === 0 && questions.length === 0}
            approval={approval}
            onDecided={() => {
              setApprovals((list) => list.filter((a) => a.id !== approval.id));
              void loadApprovals();
            }}
          />
        ))}
        {questions.map((request, index) => (
          <QuestionCard
            key={request.id}
            sessionId={sessionId}
            request={request}
            active={index === 0}
            onAnswered={() => {
              setQuestions((list) => list.filter((r) => r.id !== request.id));
              void loadQuestions();
            }}
          />
        ))}
        {plan !== null && proposedPlan(plan) !== null ? (
          <div className={styles.planDock}>
            <PlanCard
              key={plan.id}
              sessionId={sessionId}
              plan={plan}
              now={now}
              busy={planBusy}
              act={decidePlan}
            />
          </div>
        ) : null}
        {todos !== null && todos.length > 0 ? (
          <div className={styles.todoDock}>
            <TodoList items={todos} title="To-do list" />
          </div>
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
          commands={commandMenu}
          mentions={mentionMenu}
          history={history}
          harness={{ current: detail.coding_agent ?? null, onChange: (agent) => void changeHarness(agent), busy: harnessBusy }}
          model={profile?.model ?? null}
          models={
            detail.model
              ? {
                  current: detail.model,
                  options: models?.models ?? null,
                  failed: modelsError,
                  defaultModel: models?.default_model ?? detail.default_model ?? detail.model,
                  editable: models?.editable ?? false,
                  busy: modelBusy,
                  onOpen: () => void loadModels(),
                  onChange: (model) => void changeModel(model),
                }
              : undefined
          }
          browser={{
            current: detail.browser_target === "my_chrome" ? "my_chrome" : "cloud",
            connected: chromeConnected,
            failed: chromeFailed,
            editable: detail.can_manage ?? false,
            busy: browserChoiceBusy,
            onOpen: () => void loadChrome(),
            onChange: (target) => void changeBrowser(target),
          }}
          onActivity={warm}
          images={{
            files: images,
            onAdd: (files) => {
              const next = addImages(images, files);
              setImages(next.files);
              if (next.dropped > 0) setSendError("A message has at most 4 images. The other images were not added.");
            },
            onRemove: (index) => setImages((current) => current.filter((_, i) => i !== index)),
          }}
        />
      </div>
    </div>
      {browserOpen && codeTab === null ? (
        <BrowserPanel
          live={liveBrowser}
          step={liveBrowser === null ? null : browserStep(live, liveBrowser.run_id)}
          running={liveBrowser !== null && openRuns.includes(liveBrowser.run_id)}
          modal={narrow}
          busy={browserBusy}
          error={browserError}
          onControl={(action) => void browserControl(action)}
          onStop={() => void stop()}
          onClose={closeBrowser}
        />
      ) : null}
      {codeTab !== null ? (
        <CodePanel
          sessionId={sessionId}
          tab={codeTab}
          onTab={setCodeTab}
          onClose={closeCode}
          modal={narrow}
          onRequested={() => {
            setNotice("The agent opens the pull request. Approve the pr_create card when it shows.");
            if (narrow) setCodeTab(null);
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * The working line of an open run, as Claude Code shows it: what the run does, the time since
 * the view saw it start, and the key that stops it. It ticks each second on its own, so the
 * chat does not render again.
 */
function WorkingLine({ label, since, paused }: { label: string; since: number | null; paused: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (paused) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [paused]);
  return (
    <p className={styles.working}>
      {paused ? <span className={styles.pausedDot} aria-hidden="true" /> : <span className="spinner" aria-hidden="true" />}
      <span className={styles.workingLabel} role="status">
        {label}…
      </span>
      {since !== null && !paused ? (
        <span className={styles.workingMeta} aria-hidden="true">
          {elapsedText(now - since)}
        </span>
      ) : null}
      {paused ? (
        <span className={styles.workingMeta}>Answer the card below</span>
      ) : (
        <span className={styles.workingMeta}>
          <kbd className={styles.key}>Esc</kbd> to stop
        </span>
      )}
    </p>
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

function MessageView({ message: m, sessionId }: { message: ChatMessage; sessionId: string }) {
  if (m.role === "user") {
    const pictures = m.images ?? [];
    const text = pictures.length > 0 ? withoutImageMarkers(m.text) : m.text;
    return (
      <article className={styles.user} aria-label="You">
        {pictures.length > 0 ? (
          <ul className={styles.pictures} aria-label="Images">
            {pictures.map((image) => (
              <li key={image.id}>
                <SentImage sessionId={sessionId} id={image.id} name={image.name} />
              </li>
            ))}
          </ul>
        ) : null}
        {text ? <p className={styles.userText}>{text}</p> : null}
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

/** An image of a sent message. The bytes need the token, so the page loads them as a blob. */
function SentImage({ sessionId, id, name }: { sessionId: string; id: string; name: string }) {
  const api = useApi();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    let made: string | null = null;
    api
      .getAttachment(sessionId, id)
      .then((file) => {
        if (!live) return;
        made = URL.createObjectURL(file.blob);
        setUrl(made);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
      if (made !== null) URL.revokeObjectURL(made);
    };
  }, [api, sessionId, id]);
  if (failed) return <span className={styles.pictureMissing}>{name}</span>;
  if (url === null) return <span className={`skeleton ${styles.picture}`} aria-label={`Loading ${name}`} />;
  return (
    <a href={url} target="_blank" rel="noreferrer" title={name}>
      {/* A blob URL of a private image: next/image cannot load it. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={url} alt={name} className={styles.picture} />
    </a>
  );
}

/** The links to the run traces and the export of the session. */
function SessionTools({
  sessionId,
  codeOpen,
  onCode,
  browserOpen,
  onBrowser,
}: {
  sessionId: string;
  codeOpen: boolean;
  onCode: () => void;
  browserOpen: boolean;
  onBrowser: () => void;
}) {
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
      <button type="button" className="btn btn-quiet btn-small" aria-pressed={codeOpen} onClick={onCode}>
        Code
      </button>
      <button type="button" className="btn btn-quiet btn-small" aria-pressed={browserOpen} onClick={onBrowser}>
        Browser
      </button>
      <Link href={`/s/${sessionId}/runs`} className="btn btn-quiet btn-small">
        Runs
      </Link>
      <details className={styles.menu} data-menu="export">
        <summary className="btn btn-quiet btn-small" aria-label="Export the session">
          {busy !== null ? "Exporting…" : "Export"}
        </summary>
        <div className={styles.menuList}>
          <button
            type="button"
            className="btn btn-quiet btn-small"
            disabled={busy !== null}
            aria-label="Export .md: the session as Markdown"
            onClick={(event) => {
              event.currentTarget.closest("details")?.removeAttribute("open");
              void download("markdown");
            }}
          >
            Markdown (.md)
          </button>
          <button
            type="button"
            className="btn btn-quiet btn-small"
            disabled={busy !== null}
            aria-label="Export .json: the session as JSON"
            onClick={(event) => {
              event.currentTarget.closest("details")?.removeAttribute("open");
              void download("json");
            }}
          >
            JSON (.json)
          </button>
        </div>
      </details>
      {error ? (
        <span className={styles.toolsError} role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
