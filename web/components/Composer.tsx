"use client";

// The message box of the chat, as in Claude Code and Codex: `/` opens the commands and the
// skills, `@` opens the files, the sessions, and the link forms, the up arrow brings back the
// earlier messages, and the harness menu sends the session to sama, Claude Code, or Codex.

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { IMAGE_TYPES } from "@/lib/api";
import { imageFiles } from "@/lib/images";
import type { CodingAgentName } from "@/lib/api";
import { CODING_AGENTS } from "@/lib/chat";
import {
  applyChoice,
  filterItems,
  historyStart,
  type HistoryState,
  type MenuItem,
  stepHistory,
  triggerAt,
} from "@/lib/commands";
import styles from "./Composer.module.css";

export const MAX_MESSAGE_CHARS = 32_000;

export interface ModelControl {
  /** The model that the next run uses. */
  current: string;
  /** The models of the runtime, or `null` while they load. */
  options: string[] | null;
  /** The list did not load. */
  failed?: boolean;
  /** The model without a choice of the person. */
  defaultModel: string;
  editable: boolean;
  busy?: boolean;
  /** Called when the menu opens, so that the options load. */
  onOpen: () => void;
  /** `null`: back to the default model. */
  onChange: (model: string | null) => void;
}

export interface ImageControl {
  files: readonly File[];
  onAdd: (files: File[]) => void;
  onRemove: (index: number) => void;
}

export interface HarnessControl {
  /** The coding agent of the direct mode, or `null` for sama. */
  current: CodingAgentName | null;
  onChange: (agent: CodingAgentName | null) => void;
  busy?: boolean;
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  /** Sends the text as a run at the same time as the open runs (M7.1). */
  onSendParallel?: () => void;
  /** A send is in flight. */
  sending: boolean;
  /** The agent works: the Stop button shows. */
  running: boolean;
  stopping: boolean;
  onStop: () => void;
  /** The text of the Stop button, for example "Stop all" when many runs are open. */
  stopLabel?: string;
  /** The items of the `/` menu: the commands and the skills. */
  commands?: readonly MenuItem[];
  /** The items of the `@` menu: files, sessions, and the link forms. */
  mentions?: readonly MenuItem[];
  /** The earlier messages of the person, oldest first, for the up arrow. */
  history?: readonly string[];
  /** The harness menu (sama, Claude Code, Codex). */
  harness?: HarnessControl;
  /** The model of the agent, shown next to the harness. */
  model?: string | null;
  /** The model menu of the session (like `/model` in Claude Code). */
  models?: ModelControl;
  /** The images of the next message: paste, drop, or the attach button. */
  images?: ImageControl;
  /** Called when the person focuses the box or types: the session can start its sandbox. */
  onActivity?: () => void;
}

const HARNESSES: readonly { agent: CodingAgentName | null; label: string; hint: string }[] = [
  { agent: null, label: "sama", hint: "The agent of the session, with its tools and memory" },
  { agent: "claude_code", label: CODING_AGENTS.claude_code, hint: "Each message goes to the Claude Code CLI" },
  { agent: "codex", label: CODING_AGENTS.codex, hint: "Each message goes to the Codex CLI" },
];

export function Composer({
  value,
  onChange,
  onSend,
  onSendParallel,
  sending,
  running,
  stopping,
  onStop,
  stopLabel = "Stop",
  commands = [],
  mentions = [],
  history = [],
  harness,
  model = null,
  models,
  images,
  onActivity,
}: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const ids = useId();
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  // The trigger that the person closed with Esc: the menu stays closed for it.
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [historyState, setHistoryState] = useState<HistoryState>(historyStart);

  // The field grows with the text, up to the CSS max-height.
  useEffect(() => {
    const field = ref.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight}px`;
  }, [value]);

  const trigger = triggerAt(value, Math.min(caret, value.length));
  const triggerKey = trigger ? `${trigger.kind}:${trigger.start}` : null;
  const items = useMemo(() => {
    if (trigger === null) return [];
    return filterItems(trigger.kind === "slash" ? commands : mentions, trigger.query);
  }, [trigger, commands, mentions]);
  const open = trigger !== null && items.length > 0 && dismissed !== triggerKey;
  const current = Math.min(active, Math.max(0, items.length - 1));

  const fileInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const hasImages = (images?.files.length ?? 0) > 0;
  const canSend = !sending && (value.trim() !== "" || hasImages);
  const nearLimit = value.length > MAX_MESSAGE_CHARS * 0.9;

  /** Puts an earlier message in the box. Its `/` or `@` does not open a menu. */
  const recall = (text: string, at: number) => {
    const found = triggerAt(text, text.length) ?? triggerAt(text, at);
    setDismissed(found ? `${found.kind}:${found.start}` : null);
    setText(text, at);
  };

  const setText = (text: string, at: number) => {
    onChange(text);
    setCaret(at);
    requestAnimationFrame(() => {
      const field = ref.current;
      if (!field) return;
      field.focus();
      field.setSelectionRange(at, at);
    });
  };

  const choose = (item: MenuItem) => {
    if (trigger === null) return;
    const next = applyChoice(value, Math.min(caret, value.length), trigger.start, item.insert);
    setActive(0);
    // A complete choice (for example `/undo`) still matches the trigger: close the menu, so
    // that the next Enter sends the text.
    const after = triggerAt(next.text, next.caret);
    setDismissed(after ? `${after.kind}:${after.start}` : null);
    setText(next.text, next.caret);
  };

  const send = () => {
    if (!canSend) return;
    setHistoryState(historyStart);
    setDismissed(null);
    onSend();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (open) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        setActive((current + step + items.length) % items.length);
        return;
      }
      if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
        const item = items[current];
        if (item) {
          event.preventDefault();
          choose(item);
        }
        return;
      }
      if (event.key === "Escape") {
        // Esc closes the menu. The chat does not stop the run for this key.
        event.preventDefault();
        setDismissed(triggerKey);
        return;
      }
    }
    const field = event.currentTarget;
    // On the first line (up) or the last line (down) of the text, as in a shell.
    const caretAt = field.selectionStart ?? 0;
    const atStart = field.selectionStart === field.selectionEnd && !value.slice(0, caretAt).includes("\n");
    const atEnd = field.selectionStart === field.selectionEnd && !value.slice(caretAt).includes("\n");
    if (event.key === "ArrowUp" && atStart && (value === "" || historyState.index !== null)) {
      const step = stepHistory(history, historyState, "up", value);
      if (step) {
        event.preventDefault();
        setHistoryState(step.state);
        recall(step.text, 0);
      }
      return;
    }
    if (event.key === "ArrowDown" && atEnd && historyState.index !== null) {
      const step = stepHistory(history, historyState, "down", value);
      if (step) {
        event.preventDefault();
        setHistoryState(step.state);
        recall(step.text, step.text.length);
      }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  };

  const listId = `${ids}-menu`;
  const optionId = (index: number) => `${ids}-option-${index}`;
  const shownHarness = HARNESSES.find((h) => h.agent === (harness?.current ?? null)) ?? HARNESSES[0];

  return (
    <form
      className={styles.composer}
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <div
        className={`${styles.box} ${dragging ? styles.dragging : ""}`}
        onDragOver={(event) => {
          if (!images || !event.dataTransfer.types.includes("Files")) return;
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          setDragging(false);
          if (!images) return;
          const files = imageFiles(event.dataTransfer.files);
          if (files.length === 0) return;
          event.preventDefault();
          images.onAdd(files);
        }}
      >
        {images && images.files.length > 0 ? <Thumbnails files={images.files} onRemove={images.onRemove} /> : null}
        {open ? (
          <ul id={listId} role="listbox" className={styles.menu} aria-label={trigger?.kind === "slash" ? "Commands" : "Mentions"}>
            {items.map((item, index) => (
              <li
                key={item.key}
                id={optionId(index)}
                role="option"
                aria-selected={index === current}
                className={styles.option}
                onMouseDown={(event) => {
                  // Keep the focus in the field.
                  event.preventDefault();
                  choose(item);
                }}
                onMouseEnter={() => setActive(index)}
              >
                {index === 0 || items[index - 1]?.group !== item.group ? (
                  <span className={styles.group}>{item.group}</span>
                ) : null}
                <span className={styles.optionLabel}>{item.label}</span>
                <span className={styles.optionHint}>{item.hint}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <label className="sr-only" htmlFor="message">
          Message
        </label>
        <textarea
          id="message"
          ref={ref}
          className={`input ${styles.field}`}
          rows={1}
          value={value}
          maxLength={MAX_MESSAGE_CHARS}
          placeholder={running ? "Add to the current run, or type /bg for a second run" : "Message the agent. Type / for commands, @ for files"}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          aria-activedescendant={open ? optionId(current) : undefined}
          onChange={(event) => {
            const text = event.target.value;
            const at = event.target.selectionStart ?? text.length;
            onActivity?.();
            onChange(text);
            setCaret(at);
            setActive(0);
            // A closed menu opens again for a new token.
            const found = triggerAt(text, at);
            if (found === null || `${found.kind}:${found.start}` !== dismissed) setDismissed(null);
            if (historyState.index !== null) setHistoryState(historyStart);
          }}
          onSelect={(event) => setCaret(event.currentTarget.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            if (!images) return;
            const files = imageFiles(event.clipboardData.files);
            if (files.length === 0) return;
            event.preventDefault();
            images.onAdd(files);
          }}
          onBlur={() => setDismissed(triggerKey)}
          onFocus={() => {
            setDismissed(null);
            onActivity?.();
          }}
        />
        <div className={styles.bar}>
          <div className={styles.tools}>
            {images ? (
              <>
                <button
                  type="button"
                  className={styles.iconButton}
                  aria-label="Add images"
                  title="Add images (or paste or drop them)"
                  onClick={() => fileInput.current?.click()}
                >
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
                  </svg>
                </button>
                <input
                  ref={fileInput}
                  type="file"
                  accept={IMAGE_TYPES.join(",")}
                  multiple
                  hidden
                  onChange={(event) => {
                    const files = imageFiles(event.target.files ?? []);
                    if (files.length > 0) images.onAdd(files);
                    event.target.value = "";
                  }}
                />
              </>
            ) : null}
            {harness ? (
              <details className={styles.picker} data-menu="harness">
                <summary className={styles.pill} aria-label={`Harness: ${shownHarness?.label}. Change the harness`}>
                  {harness.busy ? <span className="spinner" aria-hidden="true" /> : null}
                  {shownHarness?.label}

                </summary>
                <div className={styles.pickerList} role="menu">
                  {HARNESSES.map((h) => (
                    <button
                      key={h.label}
                      type="button"
                      role="menuitemradio"
                      aria-checked={h.agent === harness.current}
                      className={styles.pickerItem}
                      disabled={harness.busy}
                      onClick={(event) => {
                        event.currentTarget.closest("details")?.removeAttribute("open");
                        if (h.agent !== harness.current) harness.onChange(h.agent);
                      }}
                    >
                      <span className={styles.optionLabel}>{h.label}</span>
                      <span className={styles.optionHint}>{h.hint}</span>
                    </button>
                  ))}
                </div>
              </details>
            ) : model && !models ? (
              <span className={styles.model}>{model}</span>
            ) : null}
            {models && harness?.current == null ? <ModelMenu control={models} /> : null}
          </div>
          <div className={styles.actions}>
            {running ? (
              <button type="button" className="btn btn-danger btn-small" onClick={onStop} disabled={stopping}>
                {stopping ? "Stopping…" : stopLabel}
              </button>
            ) : null}
            {running && onSendParallel ? (
              <button
                type="button"
                className="btn btn-small"
                disabled={!canSend}
                onClick={onSendParallel}
                title="Start a second run at the same time, in the same session"
              >
                Run in parallel
              </button>
            ) : null}
            <button type="submit" className="btn btn-primary btn-small" disabled={!canSend}>
              {sending ? "Sending…" : "Send"}
            </button>
          </div>
        </div>
      </div>
      <p className={styles.hint}>
        {running ? (
          <>
            <kbd className={styles.key}>Esc</kbd> to stop. A new message joins the current run.
          </>
        ) : (
          <>
            <kbd className={styles.key}>Enter</kbd> to send, <kbd className={styles.key}>Shift+Enter</kbd> for a new
            line, <kbd className={styles.key}>/</kbd> for commands, <kbd className={styles.key}>@</kbd> for files and
            sessions, <kbd className={styles.key}>↑</kbd> for earlier messages.
          </>
        )}
        {nearLimit ? ` ${value.length.toLocaleString()} of ${MAX_MESSAGE_CHARS.toLocaleString()} characters.` : ""}
      </p>
    </form>
  );
}

/** The images of the next message, with a remove button each. */
function Thumbnails({ files, onRemove }: { files: readonly File[]; onRemove: (index: number) => void }) {
  return (
    <ul className={styles.thumbs} aria-label="Images of the next message">
      {files.map((file, index) => (
        <li key={`${file.name}-${file.size}-${file.lastModified}-${index}`} className={styles.thumb}>
          <Preview file={file} />
          <button
            type="button"
            className={styles.thumbRemove}
            aria-label={`Remove ${file.name}`}
            onClick={() => onRemove(index)}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M3 3l6 6M9 3L3 9" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
            </svg>
          </button>
        </li>
      ))}
    </ul>
  );
}

/** A small preview of a local image file (a data URL, so nothing must be revoked). */
function Preview({ file }: { file: File }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    const reader = new FileReader();
    reader.onload = () => setUrl(typeof reader.result === "string" ? reader.result : null);
    reader.readAsDataURL(file);
    return () => reader.abort();
  }, [file]);
  // A local preview: next/image cannot load a data URL of a file that is not uploaded yet.
  // eslint-disable-next-line @next/next/no-img-element
  return url ? <img src={url} alt={file.name} className={styles.thumbImage} /> : null;
}

/** The model of the session, as `/model` in Claude Code. */
function ModelMenu({ control }: { control: ModelControl }) {
  return (
    <details
      className={styles.picker}
      data-menu="model"
      onToggle={(event) => {
        if (event.currentTarget.open) control.onOpen();
      }}
    >
      <summary className={styles.pill} aria-label={`Model: ${control.current}. Change the model`}>
        {control.busy ? <span className="spinner" aria-hidden="true" /> : null}
        <span className={styles.modelName}>{control.current}</span>
      </summary>
      <div className={styles.pickerList} role="menu">
        {control.options === null && control.failed ? (
          <div className={styles.menuNote}>
            <p className={styles.optionHint}>The models did not load.</p>
            <button type="button" className="btn btn-small" onClick={control.onOpen}>
              Try again
            </button>
          </div>
        ) : control.options === null ? (
          <p className={styles.optionHint}>Loading the models…</p>
        ) : !control.editable ? (
          <p className={styles.optionHint}>Only the owner of the session or an admin can change the model.</p>
        ) : (
          control.options.map((option) => (
            <button
              key={option}
              type="button"
              role="menuitemradio"
              aria-checked={option === control.current}
              className={styles.pickerItem}
              disabled={control.busy}
              onClick={(event) => {
                event.currentTarget.closest("details")?.removeAttribute("open");
                if (option !== control.current) control.onChange(option === control.defaultModel ? null : option);
              }}
            >
              <span className={styles.optionLabel}>{option}</span>
              {option === control.defaultModel ? <span className={styles.optionHint}>The default of the agent</span> : null}
            </button>
          ))
        )}
      </div>
    </details>
  );
}
