"use client";

import { useParams } from "next/navigation";
import { AgentSettings } from "@/components/AgentSettings";

export default function AgentPage() {
  const { profileId } = useParams<{ profileId: string }>();
  return <AgentSettings key={profileId} profileId={profileId} />;
}
