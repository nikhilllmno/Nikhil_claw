"use client";

// Small pieces that the sections of the console share: the box of a value to copy.

import { useState } from "react";
import styles from "./kit.module.css";

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
