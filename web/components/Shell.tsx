"use client";

// The signed-in frame: the sidebar (a drawer on narrow screens) and the main pane.

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "./AuthProvider";
import { DataProvider } from "./DataProvider";
import { Sidebar } from "./Sidebar";
import { AuthStatusScreen, LoadingState } from "./ui";
import { useDrawerFocus } from "./useDrawerFocus";
import styles from "./Shell.module.css";

/** The width below which the sidebar is a drawer. The same value as in the CSS. */
const DRAWER_QUERY = "(max-width: 52rem)";

function subscribeNarrow(notify: () => void) {
  const query = window.matchMedia(DRAWER_QUERY);
  query.addEventListener("change", notify);
  return () => query.removeEventListener("change", notify);
}

export function Shell({ children }: { children: React.ReactNode }) {
  const { status, retry } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status.kind === "signed-out") router.replace("/login");
  }, [status.kind, router]);

  if (status.kind === "signed-out") return <LoadingState label="Loading" />;
  return (
    <AuthStatusScreen status={status} onRetry={retry} loadingLabel="Loading">
      <DataProvider>
        <Frame>{children}</Frame>
      </DataProvider>
    </AuthStatusScreen>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const narrow = useSyncExternalStore(
    subscribeNarrow,
    () => window.matchMedia(DRAWER_QUERY).matches,
    () => false,
  );
  // The drawer is modal only when the sidebar is a drawer.
  const modal = open && narrow;
  const drawer = useRef<HTMLElement>(null);
  useDrawerFocus(modal, drawer, close);

  // A new page, or a wide screen, closes the drawer.
  const [lastPath, setLastPath] = useState(pathname);
  if (pathname !== lastPath) {
    setLastPath(pathname);
    setOpen(false);
  }
  if (open && !narrow) setOpen(false);

  return (
    <div className={styles.shell}>
      <Sidebar ref={drawer} open={open} modal={modal} onClose={close} />
      {open ? <div className={styles.overlay} onClick={close} aria-hidden="true" /> : null}
      <div className={styles.main} inert={modal}>
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
