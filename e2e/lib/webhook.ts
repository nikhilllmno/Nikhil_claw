// Standard Webhooks signatures, as the sama trigger endpoint checks them
// (src/sama/triggers/signature.py): HMAC-SHA256 of "<id>.<timestamp>.<body>" with the
// base64-decoded part of the secret after "whsec_".

import { createHmac } from "node:crypto";

export function webhookKey(secret: string): Buffer {
  const raw = secret.startsWith("whsec_") ? secret.slice("whsec_".length) : secret;
  return Buffer.from(raw, "base64");
}

export function signWebhook(secret: string, id: string, timestamp: number, body: string): string {
  const digest = createHmac("sha256", webhookKey(secret))
    .update(`${id}.${timestamp}.${body}`)
    .digest("base64");
  return `v1,${digest}`;
}

export function webhookHeaders(
  secret: string,
  id: string,
  body: string,
  now: () => number = Date.now,
): Record<string, string> {
  const timestamp = Math.floor(now() / 1000);
  return {
    "content-type": "application/json",
    "webhook-id": id,
    "webhook-timestamp": String(timestamp),
    "webhook-signature": signWebhook(secret, id, timestamp, body),
  };
}
