import { describe, expect, it } from "vitest";
import { answerOf, answersOf, emptyPicks, keyTarget, optionKey, toggle } from "./questions";

const single = { choices: ["Tonight", "Weekend"] };
const multi = { choices: ["Parking", "Breakfast", "Gym"], multi_select: true };
const free = { choices: [] as string[] };
const none = emptyPicks(1)[0]!;
const key = (k: string) => ({ key: k, ctrlKey: false, metaKey: false, altKey: false, repeat: false });

describe("question answers", () => {
  it("takes one choice on a single-select question", () => {
    let pick = toggle(single, none, 0);
    pick = toggle(single, pick, 1);
    expect(answerOf(single, pick)).toBe("Weekend");
  });

  it("takes the typed text for Other, and Other replaces a single choice", () => {
    let pick = toggle(single, toggle(single, none, 0), 2);
    expect(answerOf(single, pick)).toBeNull();
    pick = { ...pick, text: "  Next Friday " };
    expect(answerOf(single, pick)).toBe("Next Friday");
  });

  it("joins the choices of a multi-select question in their order, with Other last", () => {
    let pick = toggle(multi, none, 2);
    pick = toggle(multi, pick, 0);
    expect(answerOf(multi, pick)).toBe("Parking, Gym");
    pick = toggle(multi, pick, 2);
    pick = { ...toggle(multi, pick, 3), text: "Late check-out" };
    expect(answerOf(multi, pick)).toBe("Parking, Late check-out");
    expect(answerOf(multi, { ...pick, text: " " })).toBeNull();
  });

  it("takes a free text when the question has no choices", () => {
    expect(answerOf(free, { ...none, text: "March 3" })).toBe("March 3");
    expect(answerOf(free, none)).toBeNull();
    expect(answerOf(free, { ...none, text: "x".repeat(2001) })).toBeNull();
  });

  it("gives all answers only when each question has one, and keys act on the first open one", () => {
    const questions = [single, free];
    const picks = emptyPicks(2);
    expect(answersOf(questions, picks)).toBeNull();
    expect(keyTarget(questions, picks)).toBe(0);
    picks[0] = toggle(single, picks[0]!, 0);
    expect(keyTarget(questions, picks)).toBe(1);
    picks[1] = { ...picks[1]!, text: "two" };
    expect(answersOf(questions, picks)).toEqual(["Tonight", "two"]);
    expect(keyTarget(questions, picks)).toBe(1);
  });
});

describe("option keys", () => {
  it("maps 1 to N to the options and N+1 to Other", () => {
    expect(optionKey(key("1"), null, single)).toBe(0);
    expect(optionKey(key("3"), null, single)).toBe(2);
    expect(optionKey(key("4"), null, single)).toBeNull();
    expect(optionKey(key("a"), null, single)).toBeNull();
  });

  it("ignores keys with modifiers, in a text field, and on a free question", () => {
    expect(optionKey({ ...key("1"), ctrlKey: true }, null, single)).toBeNull();
    expect(optionKey(key("1"), { tagName: "INPUT", value: "x" }, single)).toBeNull();
    expect(optionKey(key("1"), null, free)).toBeNull();
  });
});
