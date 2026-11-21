"use client";

// The Schedules section: the scheduled jobs of the org inside the console. A job runs an agent,
// or a script, on a schedule. The list, the detail with the fires, the form, and the actions
// (pause, resume, run now, cancel, approve, remove) are the ones of the Jobs page.

import { can } from "@/lib/console";
import { Jobs } from "../../Jobs";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";

export function SchedulesSection() {
  const { org } = useConsole();
  return (
    <>
      <SectionHeader id="schedules" />
      <Jobs embedded canCreate={can(org, "jobs.create")} />
    </>
  );
}
