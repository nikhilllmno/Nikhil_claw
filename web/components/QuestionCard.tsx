"use client";

import { useEffect, useRef, useState } from "react";
import { useApi } from "./AuthProvider";
import { type ClarifyRequest, errorText } from "@/lib/api";
import { behindModal } from "@/lib/approvalKeys";
import {
  MAX_ANSWER_CHARS,
  OTHER_LABEL,
  type QuestionPick,
  answersOf,
  emptyPicks,
  keyTarget,
  optionKey,
  toggle,
} from "@/lib/questions";
import styles from "./Cards.module.css";

// One card for each question call (M8.3), as the question prompt of Claude Code: each question
// has numbered options with a short description, "Other" with a free text is always the last
// option, and a multi-select question takes more than one option. Keys 1 to 4 pick an option of
// the first question without an answer; Enter in the card sends the answers.
export function QuestionCard({
  sessionId,
  request,
  active = false,
  onAnswered,
}: {
  sessionId: string;
  request: ClarifyRequest;
  active?: boolean;
  onAnswered: () => void;
}) {
  const api = useApi();
  const [picks, setPicks] = useState<QuestionPick[]>(() => emptyPicks(request.questions.length));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const card = useRef<HTMLFormElement>(null);
  const otherFields = useRef<(HTMLInputElement | null)[]>([]);
  const answers = answersOf(request.questions, picks);

  const choose = (index: number, option: number) => {
    const question = request.questions[index];
    if (!question || busy) return;
    const isOther = option === question.choices.length;
    setPicks((old) => old.map((p, j) => (j === index ? toggle(question, p, option) : p)));
    if (isOther) window.setTimeout(() => otherFields.current[index]?.focus(), 0);
  };
  const type = (index: number, text: string) =>
    setPicks((old) => old.map((p, j) => (j === index ? { ...p, text } : p)));

  const chooseRef = useRef(choose);
  const picksRef = useRef(picks);
  useEffect(() => {
    chooseRef.current = choose;
    picksRef.current = picks;
  });

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const target = keyTarget(request.questions, picksRef.current);
      const option = optionKey(event, document.activeElement, request.questions[target]);
      if (option === null || behindModal(card.current, document)) return;
      event.preventDefault();
      chooseRef.current(target, option);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, request.questions]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (answers === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.answerQuestion(sessionId, request.id, answers);
      onAnswered();
    } catch (e) {
      setError(errorText(e, "The answer was not saved."));
      setBusy(false);
    }
  };

  const many = request.questions.length > 1;
  return (
    <form
      ref={card}
      className={`card ${styles.card}`}
      onSubmit={submit}
      aria-label={many ? "Questions from the agent" : "Question from the agent"}
    >
      {request.questions.map((q, i) => {
        const pick = picks[i] ?? emptyPicks(1)[0]!;
        const multi = q.multi_select === true;
        if (q.choices.length === 0) {
          return (
            <label key={i} className={`field ${styles.question}`}>
              {q.header ? <span className={styles.header}>{q.header}</span> : null}
              <span className={styles.title}>{q.question}</span>
              <input
                className="input"
                required
                maxLength={MAX_ANSWER_CHARS}
                value={pick.text}
                disabled={busy}
                onChange={(e) => type(i, e.target.value)}
              />
            </label>
          );
        }
        return (
          <div key={i} className={styles.question} role="group" aria-labelledby={`${request.id}-q${i}`}>
            {q.header ? <span className={styles.header}>{q.header}</span> : null}
            <h2 id={`${request.id}-q${i}`} className={styles.title}>
              {q.question}
            </h2>
            {multi ? <p className={styles.hint}>Choose one or more.</p> : null}
            <div className={styles.options}>
              {[...q.choices, OTHER_LABEL].map((label, n) => {
                const isOther = n === q.choices.length;
                const on = isOther ? pick.other : pick.chosen.includes(label);
                const description = isOther ? "Type your own answer" : q.descriptions?.[n];
                return (
                  <button
                    key={`${n}-${label}`}
                    type="button"
                    role={multi ? "checkbox" : "radio"}
                    aria-checked={on}
                    className={`${styles.option} ${on ? styles.optionOn : ""}`}
                    disabled={busy}
                    onClick={() => choose(i, n)}
                  >
                    <span className={styles.optionKey} aria-hidden="true">
                      {n + 1}
                    </span>
                    <span className={styles.optionText}>
                      <span className={styles.optionLabel}>{label}</span>
                      {description ? <span className={styles.optionDescription}>{description}</span> : null}
                    </span>
                  </button>
                );
              })}
            </div>
            {pick.other ? (
              <input
                ref={(el) => {
                  otherFields.current[i] = el;
                }}
                className="input"
                aria-label={`Your answer: ${q.question}`}
                placeholder="Type your answer"
                required
                maxLength={MAX_ANSWER_CHARS}
                value={pick.text}
                disabled={busy}
                onChange={(e) => type(i, e.target.value)}
              />
            ) : null}
          </div>
        );
      })}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || answers === null}>
          {busy ? "Sending…" : "Submit"}
        </button>
      </div>
    </form>
  );
}
