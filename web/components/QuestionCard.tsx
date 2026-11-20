"use client";

import { useState } from "react";
import { useApi } from "./AuthProvider";
import { type ClarifyRequest, errorText } from "@/lib/api";
import styles from "./Cards.module.css";

export function QuestionCard({
  sessionId,
  request,
  onAnswered,
}: {
  sessionId: string;
  request: ClarifyRequest;
  onAnswered: () => void;
}) {
  const api = useApi();
  const [answers, setAnswers] = useState<string[]>(() =>
    request.questions.map((q) => q.choices[0] ?? ""),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
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

  return (
    <form className={`card ${styles.card}`} onSubmit={submit} aria-label="Question from the agent">
      <h2 className={styles.title}>The agent has a question</h2>
      {request.questions.map((q, i) => (
        <label key={i} className="field">
          <span className="label">{q.question}</span>
          {q.choices.length > 0 ? (
            <select
              className="input"
              value={answers[i]}
              disabled={busy}
              onChange={(e) => setAnswers(answers.map((a, j) => (j === i ? e.target.value : a)))}
            >
              {q.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {choice}
                </option>
              ))}
            </select>
          ) : (
            <input
              className="input"
              required
              maxLength={2000}
              value={answers[i]}
              disabled={busy}
              onChange={(e) => setAnswers(answers.map((a, j) => (j === i ? e.target.value : a)))}
            />
          )}
        </label>
      ))}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className={styles.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Sending…" : "Answer"}
        </button>
      </div>
    </form>
  );
}
