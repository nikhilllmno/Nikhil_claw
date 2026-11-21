"use client";

import { useParams } from "next/navigation";
import { ConsoleFrame } from "@/components/console/Console";
import { SECTION_SCREENS } from "@/components/console/sections";
import { sectionById } from "@/lib/console";

export default function ConsoleSectionPage() {
  const { section: id } = useParams<{ section: string }>();
  const section = sectionById(id);
  const Screen = section ? SECTION_SCREENS[section.id] : null;
  return <ConsoleFrame section={section}>{Screen ? <Screen /> : null}</ConsoleFrame>;
}
