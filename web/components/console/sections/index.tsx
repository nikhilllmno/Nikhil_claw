"use client";

// The screen of each section of the console.

import type { SectionId } from "@/lib/console";
import { ApiKeysSection } from "./ApiKeysSection";
import { HarnessSection } from "./HarnessSection";
import { McpServersSection } from "./McpServersSection";
import { RolesSection } from "./RolesSection";
import { SchedulesSection } from "./SchedulesSection";
import { SessionsSection } from "./SessionsSection";
import { SetupSection } from "./SetupSection";
import { SkillsSection } from "./SkillsSection";
import { SlackSection } from "./SlackSection";
import { SubagentsSection } from "./SubagentsSection";
import { ToolsSection } from "./ToolsSection";
import { TriggersSection } from "./TriggersSection";
import { WebhooksSection } from "./WebhooksSection";
import { WorkflowsSection } from "./WorkflowsSection";

export const SECTION_SCREENS: Record<SectionId, () => React.ReactNode> = {
  setup: SetupSection,
  slack: SlackSection,
  roles: RolesSection,
  "api-keys": ApiKeysSection,
  harness: HarnessSection,
  tools: ToolsSection,
  subagents: SubagentsSection,
  skills: SkillsSection,
  mcp: McpServersSection,
  webhooks: WebhooksSection,
  triggers: TriggersSection,
  schedules: SchedulesSection,
  workflows: WorkflowsSection,
  sessions: SessionsSection,
};
