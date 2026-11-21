"use client";

import { useEffect, useRef } from "react";
import styles from "./Composer.module.css";

export const MAX_MESSAGE_CHARS = 32_000;

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  /** A send is in flight. */
  sending: boolean;
  /** The agent works: the Stop button shows. */
  running: boolean;
  stopping: boolean;
  onStop: () => void;
}

export function Composer({ value, onChange, onSend, sending, running, stopping, onStop }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // The field grows with the text, up to the CSS max-height.
  useEffect(() => {
    const field = ref.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${field.scrollHeight}px`;
  }, [value]);

  const canSend = !sending && value.trim() !== "";
  const nearLimit = value.length > MAX_MESSAGE_CHARS * 0.9;

  return (
    <form
      className={styles.composer}
      onSubmit={(event) => {
        event.preventDefault();
        if (canSend) onSend();
      }}
    >
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
        placeholder="Message the agent"
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (canSend) onSend();
          }
        }}
      />
      <div className={styles.actions}>
        {running ? (
          <button type="button" className="btn btn-danger" onClick={onStop} disabled={stopping}>
            {stopping ? "Stopping…" : "Stop"}
          </button>
        ) : null}
        <button type="submit" className="btn btn-primary" disabled={!canSend}>
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
      <p className={styles.hint}>
        Enter to send, Shift+Enter for a new line. Type /compact to shorten the context, /steer TEXT to guide the
        current run, or /handoff telegram|slack to move the session.
        {nearLimit ? ` ${value.length.toLocaleString()} of ${MAX_MESSAGE_CHARS.toLocaleString()} characters.` : ""}
      </p>
    </form>
  );
}
