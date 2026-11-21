"use client";

// The page of an invite link (docs/architecture.md, Section 17.2). The token is in the URL
// fragment, so it does not go to a server log. The page keeps the token in localStorage for one
// hour, so that it works after the user signs in or creates an account.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/components/AuthProvider";
import { AuthStatusScreen } from "@/components/ui";
import { ApiError, errorText } from "@/lib/api";
import { forgetPendingInvite, inviteToken, keepPendingInvite, pendingInvite } from "@/lib/orgs";
import styles from "../login/login.module.css";

export default function InvitePage() {
  const { status, api, signOut, retry } = useAuth();
  const router = useRouter();
  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const read = useRef(false);

  // Read the token once: from the link, or the one kept before the sign-in.
  useEffect(() => {
    if (read.current) return;
    read.current = true;
    const store = window.localStorage;
    const hash = window.location.hash;
    const found = inviteToken(hash, () => pendingInvite(store, Date.now()));
    if (hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
      if (found !== null) keepPendingInvite(store, found, Date.now());
    }
    // The fragment exists only in the browser, so the page reads it after the first render.
    setToken(found);
  }, []);

  const join = async () => {
    if (!api || !token) return;
    setBusy(true);
    setError(null);
    try {
      await api.acceptInvite({ token });
      forgetPendingInvite(window.localStorage);
      router.replace("/");
    } catch (e) {
      // A used, ended, or unknown invite does not come back: forget it.
      if (e instanceof ApiError && (e.status === 404 || e.status === 410 || e.status === 409)) {
        forgetPendingInvite(window.localStorage);
      }
      setError(errorText(e, "Could not join the org."));
      setBusy(false);
    }
  };

  const signedIn = status.kind === "signed-in";
  return (
    <AuthStatusScreen status={status} onRetry={retry} loadingLabel="Connecting">
      <main className={styles.page}>
        <div className={styles.panel}>
          <h1 className={styles.brand}>sama</h1>
          {token === undefined ? null : token === null ? (
            <>
              <p className={styles.tagline}>This invite link is not complete or is too old.</p>
              <p className="hint">Ask the admin of the org for a new link.</p>
              <Link href="/" className="btn">
                Go to sama
              </Link>
            </>
          ) : (
            <>
              <p className={styles.tagline}>You have an invite to join an org.</p>
              {signedIn ? (
                <>
                  <p className="hint">The invite is for one email address. Join with the account of that address.</p>
                  <button type="button" className="btn btn-primary" onClick={join} disabled={busy}>
                    {busy ? "Joining…" : "Join the org"}
                  </button>
                  <button type="button" className="btn btn-quiet" onClick={signOut} disabled={busy}>
                    Use another account
                  </button>
                </>
              ) : (
                <>
                  <p className="hint">
                    Sign in, or create an account, with the email address that got the invite. Then
                    you come back here.
                  </p>
                  <Link href="/login" className="btn btn-primary">
                    Sign in or create an account
                  </Link>
                </>
              )}
            </>
          )}
          <div aria-live="polite">{error ? <p className="alert alert-error">{error}</p> : null}</div>
        </div>
      </main>
    </AuthStatusScreen>
  );
}
