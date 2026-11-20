// Time helpers for the session list.

export type AgeGroup = "Today" | "Yesterday" | "Previous 7 days" | "Older";

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(ms: number): number {
  const date = new Date(ms);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

export function ageGroup(createdAt: string, now: number): AgeGroup {
  const created = Date.parse(createdAt);
  if (Number.isNaN(created)) return "Older";
  const today = startOfDay(now);
  if (created >= today) return "Today";
  if (created >= today - DAY_MS) return "Yesterday";
  if (created >= today - 7 * DAY_MS) return "Previous 7 days";
  return "Older";
}

/** Groups items (newest first) by age. A group without items is not in the result. */
export function groupByAge<T extends { created_at: string }>(
  items: readonly T[],
  now: number,
): { group: AgeGroup; items: T[] }[] {
  const order: AgeGroup[] = ["Today", "Yesterday", "Previous 7 days", "Older"];
  const buckets = new Map<AgeGroup, T[]>(order.map((group) => [group, []]));
  for (const item of items) buckets.get(ageGroup(item.created_at, now))?.push(item);
  return order.flatMap((group) => {
    const list = buckets.get(group) ?? [];
    return list.length > 0 ? [{ group, items: list }] : [];
  });
}

/** For example "5 min ago" or "3 d ago". */
export function relativeTime(iso: string, now: number): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return new Date(then).toLocaleDateString();
}
