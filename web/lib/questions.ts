// The answer state of a question card (M8.3), as in the question prompt of Claude Code. Each
// question with choices shows them as numbered options; "Other" with a free text is always the
// last option. A multi-select question takes one or more options. A question without choices
// takes a free text only. Pure, so tests can run it.

import { typingInField } from "./approvalKeys";

export const OTHER_LABEL = "Other";
export const MAX_ANSWER_CHARS = 2000;
export const MULTI_SEPARATOR = ", ";

export type QuestionPick = { chosen: string[]; other: boolean; text: string };

export interface QuestionLike {
  choices: readonly string[];
  multi_select?: boolean;
}

export function emptyPicks(count: number): QuestionPick[] {
  return Array.from({ length: count }, () => ({ chosen: [], other: false, text: "" }));
}

/** The pick after a click on choice `index` (from 0); `index === choices.length` is "Other". */
export function toggle(question: QuestionLike, pick: QuestionPick, index: number): QuestionPick {
  const multi = question.multi_select === true;
  if (index === question.choices.length) {
    return multi ? { ...pick, other: !pick.other } : { chosen: [], other: true, text: pick.text };
  }
  const choice = question.choices[index];
  if (choice === undefined) return pick;
  if (!multi) return { chosen: [choice], other: false, text: pick.text };
  const chosen = pick.chosen.includes(choice)
    ? pick.chosen.filter((c) => c !== choice)
    : question.choices.filter((c) => c === choice || pick.chosen.includes(c));
  return { ...pick, chosen };
}

/** The answer of one question, or null when it has none yet. */
export function answerOf(question: QuestionLike, pick: QuestionPick): string | null {
  const typed = pick.text.trim();
  if (question.choices.length === 0 || pick.other) {
    if (!typed || typed.length > MAX_ANSWER_CHARS) return null;
    if (question.choices.length === 0 || question.multi_select !== true) return typed;
  }
  const parts = [...pick.chosen.filter((c) => question.choices.includes(c))];
  if (pick.other) parts.push(typed);
  if (parts.length === 0) return null;
  const answer = parts.join(MULTI_SEPARATOR);
  return answer.length <= MAX_ANSWER_CHARS ? answer : null;
}

/** The answers of all questions, or null while one question has no answer. */
export function answersOf(questions: readonly QuestionLike[], picks: readonly QuestionPick[]): string[] | null {
  const answers = questions.map((q, i) => answerOf(q, picks[i] ?? emptyPicks(1)[0]!));
  return answers.every((a): a is string => a !== null) ? answers : null;
}

/** The question that the number keys act on: the first without an answer, else the last. */
export function keyTarget(questions: readonly QuestionLike[], picks: readonly QuestionPick[]): number {
  const open = questions.findIndex((q, i) => answerOf(q, picks[i] ?? emptyPicks(1)[0]!) === null);
  return open === -1 ? questions.length - 1 : open;
}

/**
 * The option of a key press on the active question card, from 0 (`choices.length` is "Other"),
 * or null. Keys 1 to 4 pick the options, and the key after the last option picks "Other". A key
 * in a text field is text.
 */
export function optionKey(
  event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey" | "repeat"> &
    Partial<Pick<KeyboardEvent, "defaultPrevented" | "isComposing">>,
  active: Parameters<typeof typingInField>[0],
  question: QuestionLike | undefined,
): number | null {
  if (!question || question.choices.length === 0) return null;
  if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return null;
  if (event.defaultPrevented || event.isComposing) return null;
  if (typingInField(active)) return null;
  if (!/^[1-9]$/.test(event.key)) return null;
  const index = Number(event.key) - 1;
  return index <= question.choices.length ? index : null;
}
