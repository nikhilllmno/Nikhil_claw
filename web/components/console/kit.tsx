"use client";

// Small pieces that the sections of the console share: a badge for a tone, the box of a value
// to copy, and the choice of an agent.

import { useState } from "react";
import type { Org } from "@/lib/api";
import type { Tone } from "@/lib/jobs";
import { useData } from "../DataProvider";
import { useConsole } from "./Console";
import styles from "./kit.module.css";

const TONE_CLASS: Record<Tone, string> = {
  accent: "badge badge-accent",
  warning: "badge badge-warning",
  danger: "badge badge-danger",
  neutral: "badge",
};

export function ToneBadge({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={TONE_CLASS[tone]}>{children}</span>;
}

/** A value in a read-only box, with a button that copies it. */
export function CopyBox({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch {
      setState("failed");
    }
  };
  return (
    <div className={styles.copyBox}>
      <div className={styles.copyRow}>
        <code className={styles.value} aria-label={label}>
          {value}
        </code>
        <button type="button" className="btn btn-small" onClick={copy}>
          {state === "copied" ? "Copied" : "Copy"}
        </button>
      </div>
      {state === "failed" ? (
        <p className="alert alert-error" role="alert">
          Could not copy. Select the text and copy it.
        </p>
      ) : null}
    </div>
  );
}

/**
 * The agent that a section works on. It starts with the first agent of the workspace, and the
 * choice stays while the agent is in the list.
 */
export function useAgentChoice(): {
  org: Org;
  workspaceId: string;
  profileId: string | null;
  setProfileId: (id: string) => void;
} {
  const { org, workspaceId } = useConsole();
  const { profilesFor } = useData();
  const [picked, setPicked] = useState<string | null>(null);
  const profiles = org.agent_profiles;
  const profileId =
    picked !== null && profiles.some((p) => p.id === picked)
      ? picked
      : (profilesFor(workspaceId)[0]?.id ?? profiles[0]?.id ?? null);
  return { org, workspaceId, profileId, setProfileId: setPicked };
}
