// The names of the test users and orgs. One prefix marks all test data, so the sweep can find
// and remove what a failed run left behind (scripts/sweep.ts).

export const EMAIL_PREFIX = "sama-e2e+";
/** A reserved domain (RFC 2606). No mail goes anywhere. */
export const EMAIL_DOMAIN = "example.com";
export const ORG_PREFIX = "e2e-";

export function testEmail(runId: string, role: string): string {
  return `${EMAIL_PREFIX}${runId}-${role}@${EMAIL_DOMAIN}`.toLowerCase();
}

export function isTestEmail(email: string | null | undefined): boolean {
  if (!email) return false;
  const lower = email.toLowerCase();
  return lower.startsWith(EMAIL_PREFIX) && lower.endsWith(`@${EMAIL_DOMAIN}`);
}

export function orgName(runId: string, label = "main"): string {
  return `${ORG_PREFIX}${runId}-${label}`;
}

/** A unique word that a test puts in a message and then looks for. */
export function marker(label: string, random: () => number = Math.random): string {
  const suffix = Math.floor(random() * 36 ** 6)
    .toString(36)
    .padStart(6, "0");
  return `${label.toUpperCase()}${suffix.toUpperCase()}`;
}

/** A strong password for a test user. It lives only in the world file of the run. */
export function newPassword(bytes: (n: number) => Uint8Array): string {
  return `E2e-${Buffer.from(bytes(18)).toString("base64url")}`;
}
