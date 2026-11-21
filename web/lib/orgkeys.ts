// The model keys and the region of an org on the settings page (docs/architecture.md,
// Section 17.3). The key goes only into the form of the page. The API never sends it back.

import type { ModelKeyStatus, ModelProvider, Org } from "./api";

export const PROVIDER_TITLES: Record<ModelProvider, string> = { anthropic: "Anthropic", openai: "OpenAI" };

const PREFIX: Record<ModelProvider, string> = { anthropic: "sk-ant-", openai: "sk-" };
export const MIN_KEY_CHARS = 20;
export const MAX_KEY_CHARS = 512;

/** The same check as the API, so that a wrong paste fails before it leaves the page. */
export function keyProblem(provider: ModelProvider, key: string): string | null {
  const value = key.trim();
  if (value === "") return `Give the ${PROVIDER_TITLES[provider]} key.`;
  if (value.length < MIN_KEY_CHARS || value.length > MAX_KEY_CHARS || /\s/.test(value)) {
    return `The key must have ${MIN_KEY_CHARS} to ${MAX_KEY_CHARS} characters and no spaces.`;
  }
  if (!value.startsWith(PREFIX[provider])) {
    return `An ${PROVIDER_TITLES[provider]} key starts with ${PREFIX[provider]}.`;
  }
  return null;
}

/** Which key the agents of the org use for a provider. */
export function keyUse(status: ModelKeyStatus): { badge: string; text: string } {
  if (status.org_key) return { badge: "Org key", text: "The agents use the key of the org." };
  if (status.default_key) return { badge: "Sama key", text: "The agents use the key of sama." };
  return { badge: "No key", text: "No key: the models of this provider do not work for the org." };
}

/** The region line of an org. */
export function regionText(org: Pick<Org, "region" | "region_here">): string {
  return org.region_here
    ? `The org is in region ${org.region}. This deployment serves it.`
    : `The org is in region ${org.region}. Another deployment serves it.`;
}
