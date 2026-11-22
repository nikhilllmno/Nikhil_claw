"use client";

// The API keys section (M7.6): the org API keys for the MCP server and the OpenAI-compatible
// endpoint. It reuses the card of the admin area. An org admin makes and revokes keys; a member
// sees who manages them.

import { ApiKeys } from "../../ApiKeys";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";

export function ApiKeysSection() {
  const { org } = useConsole();
  return (
    <>
      <SectionHeader id="api-keys" />
      <ApiKeys org={org} />
    </>
  );
}
