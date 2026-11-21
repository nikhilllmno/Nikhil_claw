"use client";

import { useParams } from "next/navigation";
import { CodingPanel } from "@/components/CodingPanel";

export default function CodePage() {
  const { sessionId } = useParams<{ sessionId: string }>();
  return <CodingPanel key={sessionId} sessionId={sessionId} />;
}
