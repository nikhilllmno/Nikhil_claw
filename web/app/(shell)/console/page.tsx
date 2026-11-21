"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { LoadingState } from "@/components/ui";
import { DEFAULT_SECTION } from "@/lib/console";

export default function ConsolePage() {
  const router = useRouter();
  useEffect(() => {
    router.replace(`/console/${DEFAULT_SECTION}`);
  }, [router]);
  return <LoadingState label="Opening the console" />;
}
