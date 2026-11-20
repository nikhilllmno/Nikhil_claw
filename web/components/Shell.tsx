"use client";

// The signed-in frame: the sidebar (a drawer on narrow screens) and the main pane.

import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "./AuthProvider";
import { DataProvider } from "./DataProvider";
import { Sidebar } from "./Sidebar";
import { ErrorState, LoadingState } from "./ui";
import styles from "./Shell.module.css";

export function Shell({ children }: { children: React.ReactNode }) {
  const { status, retry } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status.kind === "signed-out") router.replace("/login");
  }, [status.kind, router]);

  if (status.kind === "loading" || status.kind === "signed-out") {
    return <LoadingState label="Loading" />;
  }
  if (status.kind === "unconfigured") {
    return (
      <ErrorState
        title="The app is not set up"
        message="NEXT_PUBLIC_SAMA_API_URL is not set. Set it to the address of the sama API and build the app again."
      />
    );
  }
  if (status.kind === "unreachable") {
    return <ErrorState title="Cannot reach the sama API" message={status.message} onRetry={retry} />;
  }
  return (
    <DataProvider>
      <Frame>{children}</Frame>
    </DataProvider>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);

  // A new page closes the drawer.
  const [lastPath, setLastPath] = useState(pathname);
  if (pathname !== lastPath) {
    setLastPath(pathname);
    setOpen(false);
  }

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className={styles.shell}>
      <Sidebar open={open} onClose={close} />
      {open ? <div className={styles.overlay} onClick={close} aria-hidden="true" /> : null}
      <div className={styles.main}>
        <header className={styles.topbar}>
          <button
            type="button"
            className="btn btn-quiet btn-icon"
            aria-label="Open the sidebar"
            aria-expanded={open}
            aria-controls="sidebar"
            onClick={() => setOpen(true)}
          >
            <MenuIcon />
          </button>
          <span className={styles.topbarBrand}>sama</span>
        </header>
        <div className={styles.content}>{children}</div>
      </div>
    </div>
  );
}

function MenuIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M3 5h14M3 10h14M3 15h14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}
