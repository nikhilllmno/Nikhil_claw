"use client";

import { useParams } from "next/navigation";
import { Chat } from "@/components/Chat";

export default function SessionPage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  // The key gives each session its own state.
  return <Chat key={sessionId} sessionId={sessionId} />;
}
