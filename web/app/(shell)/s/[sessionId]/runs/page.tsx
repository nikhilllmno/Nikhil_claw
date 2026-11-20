"use client";

import { useParams } from "next/navigation";
import { RunTraces } from "@/components/RunTraces";

export default function RunsPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  return <RunTraces key={sessionId} sessionId={sessionId} />;
}
