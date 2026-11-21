import { describe, expect, it } from "vitest";
import { ageGroup, groupByAge, relativeTime, timeLeft } from "./time";

const now = new Date(2026, 10, 20, 15, 0, 0).getTime();
const at = (day: number, hour: number) => new Date(2026, 10, day, hour).toISOString();

describe("ageGroup", () => {
  it("groups by calendar day", () => {
    expect(ageGroup(at(20, 1), now)).toBe("Today");
    expect(ageGroup(at(19, 23), now)).toBe("Yesterday");
    expect(ageGroup(at(15, 12), now)).toBe("Previous 7 days");
    expect(ageGroup(at(1, 12), now)).toBe("Older");
    expect(ageGroup("not a date", now)).toBe("Older");
  });
});

describe("groupByAge", () => {
  it("keeps the order and drops empty groups", () => {
    const items = [
      { created_at: at(20, 10), id: "a" },
      { created_at: at(20, 9), id: "b" },
      { created_at: at(2, 9), id: "c" },
    ];
    const groups = groupByAge(items, now);
    expect(groups.map((g) => g.group)).toEqual(["Today", "Older"]);
    expect(groups[0]?.items.map((i) => i.id)).toEqual(["a", "b"]);
  });
});

describe("relativeTime", () => {
  it("says how long ago", () => {
    expect(relativeTime(new Date(now - 10_000).toISOString(), now)).toBe("just now");
    expect(relativeTime(new Date(now - 5 * 60_000).toISOString(), now)).toBe("5 min ago");
    expect(relativeTime(new Date(now - 3 * 3_600_000).toISOString(), now)).toBe("3 h ago");
    expect(relativeTime(new Date(now - 2 * 86_400_000).toISOString(), now)).toBe("2 d ago");
    expect(relativeTime("bad", now)).toBe("");
  });
});

describe("timeLeft", () => {
  const now = Date.parse("2026-01-10T12:00:00Z");
  it("counts down to a time", () => {
    expect(timeLeft(new Date(now + 30_000).toISOString(), now)).toBe("in less than 1 min");
    expect(timeLeft(new Date(now + 5 * 60_000).toISOString(), now)).toBe("in 5 min");
    expect(timeLeft(new Date(now + 3 * 3_600_000).toISOString(), now)).toBe("in 3 h");
    expect(timeLeft(new Date(now + 7 * 86_400_000).toISOString(), now)).toBe("in 7 d");
  });
  it("says that a past time ended", () => {
    expect(timeLeft(new Date(now - 1000).toISOString(), now)).toBe("ended");
    expect(timeLeft("not a date", now)).toBe("");
  });
});
