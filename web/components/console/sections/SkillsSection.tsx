"use client";

// The Skills section: the skill editor of the workspace inside the console. The editor lists the
// skills, shows one skill with its files and versions, and makes new skills. The role of the
// user must have the permission to write skills to see the New skill button.

import { can } from "@/lib/console";
import { SkillEditor } from "../../SkillEditor";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";

export function SkillsSection() {
  const { org } = useConsole();
  const canWrite = can(org, "skills.write");
  return (
    <>
      <SectionHeader id="skills" />
      <SkillEditor embedded canWrite={canWrite} />
    </>
  );
}
